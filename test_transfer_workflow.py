import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from app import BankingAgent
from gateway import BankingToolGateway
from router import IntentType, RouteResult
from workflows.models import TransferDraft, TransferTurn
from workflows.store import WorkflowStore
from workflows.manager import WorkflowManager
from workflows.parser import TransferParser

CONTEXT = {"customer_id": "C10001", "default_account_id": "CHK-001", "default_card_id": "VISA-001"}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(Path(self.tmp.name) / 'bank.sqlite')
        self.gateway = BankingToolGateway()
        self.gateway.enable_transfers(self.store)
        self.parser = Mock()
        self.manager = self.new_manager()

    def new_manager(self, conversation='conversation'):
        manager = WorkflowManager(self.gateway, self.store, self.parser, conversation)
        self.addCleanup(manager.close)
        return manager

    def turn(self, action, changes=None, manager=None):
        return (manager or self.manager).handle('test', CONTEXT, turn=TransferTurn(
            action=action, changes=TransferDraft(**(changes or {}))))

    def start(self, **changes):
        values = {"amount": "500", "recipient_email": "dingsijie@gmail.com", **changes}
        return self.turn('start', values)

    def task(self):
        return self.manager.get_task(CONTEXT['customer_id'])

    def balance(self):
        return self.gateway.get_balance('CHK-001')['available_balance']

    def test_complete_confirm_duplicate(self):
        self.assertIn('Confirm transfer', self.start())
        self.assertEqual(self.balance(), 5320.45)
        self.assertIn('successful', self.turn('confirm'))
        self.turn('confirm')
        self.assertEqual(self.balance(), 4820.45)
        self.assertEqual(len(self.store.transactions('CHK-001')), 1)
        self.assertIn('Transfer to', self.gateway.get_transactions('CHK-001')['transactions'][0]['description'])

    def test_missing_amount_and_recipient(self):
        self.assertIn('amount', self.start(amount=None))
        self.assertIn('Confirm transfer', self.turn('update', {'amount': '500'}))
        self.assertEqual(self.task().draft.recipient_email, 'dingsijie@gmail.com')
        self.turn('cancel')
        self.assertIn('recipient email', self.start(recipient_email=None))
        self.assertIn('Confirm transfer', self.turn('update', {'recipient_email': 'abc@example.com'}))

    def test_changes_override_confirmation_and_clear(self):
        self.start()
        self.assertIn('300.00', self.turn('confirm', {'amount': '300', 'recipient_email': 'abc@example.com'}))
        self.assertEqual(self.balance(), 5320.45)
        self.assertIsNone(self.task().confirmed_version)
        self.turn('update', {'amount': None})
        self.assertIsNone(self.task().draft.amount)
        self.turn('confirm')
        self.assertEqual(self.balance(), 5320.45)

    def test_pause_query_resume(self):
        self.start()
        self.turn('pause')
        self.assertIn('paused', self.turn('confirm'))
        self.assertIsNone(self.turn('other'))
        self.assertEqual(self.task().status, 'PAUSED')
        self.assertIn('Confirm transfer', self.turn('resume'))
        self.assertEqual(self.balance(), 5320.45)
        self.turn('confirm')
        self.assertEqual(self.balance(), 4820.45)

    def test_cancel_no_debit(self):
        self.start()
        self.assertIn('cancelled', self.turn('cancel'))
        self.turn('resume')
        self.turn('confirm')
        self.assertEqual(self.balance(), 5320.45)

    def test_unrelated_then_yes_requires_fresh_confirmation(self):
        self.start()
        self.turn('other')
        self.assertIn('Confirm transfer', self.turn('confirm'))
        self.assertEqual(self.balance(), 5320.45)

    def test_ambiguous_and_parse_failure_preserve_draft(self):
        self.start()
        self.turn('clarify')
        self.assertEqual(str(self.task().draft.amount), '500')
        self.parser.parse.side_effect = ValueError('bad json')
        self.assertIn('clarify', self.manager.handle('???', CONTEXT))
        self.assertIn('Confirm transfer', self.turn('confirm'))
        self.assertEqual(self.balance(), 5320.45)

    def test_validation_and_authorization(self):
        for amount in ('-1', '0', 'NaN', 'Infinity', '1.001', 'not a number', True):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                TransferDraft(amount=amount)
        with self.assertRaises(ValueError):
            TransferDraft(recipient_email='bad-email')
        with self.assertRaises(ValueError):
            TransferDraft(currency='USD')
        self.assertIn('Unknown recipient', self.start(recipient_email='unknown@example.com'))
        self.assertIn('Unauthorized', self.turn('update', {'recipient_email': 'abc@example.com', 'source_account_id': 'CHK-002'}))
        self.assertIn('Insufficient', self.turn('update', {'source_account_id': 'CHK-001', 'amount': '9000'}))
        self.assertEqual(self.balance(), 5320.45)

    def test_second_task_not_overwritten(self):
        self.start()
        task_id = self.task().task_id
        self.assertIn('unfinished', self.start(amount='100'))
        self.assertEqual(self.task().task_id, task_id)
        self.assertEqual(self.task().draft.amount, 500)

    def test_restart_reauth_and_reset(self):
        self.start()
        other = self.new_manager()
        self.assertIn('Confirm transfer', self.turn('confirm', manager=other))
        self.assertEqual(self.balance(), 5320.45)
        self.assertIn('retained', other.reset(CONTEXT['customer_id']))
        self.assertIn('paused', self.turn('confirm', manager=other))
        self.turn('resume', manager=other)
        self.turn('confirm', manager=other)
        reloaded = BankingToolGateway()
        reloaded.enable_transfers(WorkflowStore(self.store.path))
        self.assertEqual(reloaded.get_balance('CHK-001')['available_balance'], 4820.45)
        with self.assertRaises(ValueError):
            other.handle('resume', {'customer_id': 'C10002'}, turn=TransferTurn(action='resume'))

    def test_timeout_after_success_reconciles(self):
        self.start()
        original = self.gateway.submit_transfer
        def uncertain(*args):
            original(*args)
            raise TimeoutError('lost response')
        with patch.object(self.gateway, 'submit_transfer', side_effect=uncertain):
            self.assertIn('unknown', self.turn('confirm'))
        self.assertEqual(self.task().status, 'RECONCILING')
        self.assertIn('successful', self.turn('cancel'))
        self.assertEqual(self.balance(), 4820.45)
        self.assertEqual(len(self.store.transactions('CHK-001')), 1)

    def test_unknown_outcome_no_false_cancel(self):
        self.start()
        with patch.object(self.gateway, 'submit_transfer', side_effect=TimeoutError()):
            self.turn('confirm')
        with patch.object(self.gateway, 'get_transfer_status', return_value=None):
            self.assertIn('cancellation is not confirmed', self.turn('cancel'))
        self.assertEqual(self.task().status, 'RECONCILING')
        self.assertEqual(self.balance(), 5320.45)

    def test_concurrent_confirmation_and_backend_idempotency(self):
        self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.turn('confirm'), range(2)))
        self.assertEqual(self.balance(), 4820.45)
        task = self.task()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.gateway.submit_transfer(task.owner, task.submission_key, task.validated), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.balance(), 4820.45)
        changed = task.validated.model_copy(update={'amount': task.validated.amount + 1})
        with self.assertRaises(ValueError):
            self.gateway.submit_transfer(task.owner, task.submission_key, changed)

    def test_concurrent_large_duplicate_submission(self):
        self.start(amount='5000')
        transfer = self.task().validated
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.gateway.submit_transfer('C10001', 'same-key', transfer), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['status'], 'SUCCEEDED')
        self.assertEqual(self.balance(), 320.45)

    def test_balance_changes_after_confirmation(self):
        self.start(amount='5000')
        with self.store.connect() as db:
            db.execute("UPDATE accounts SET available=100 WHERE account='CHK-001'")
        self.assertIn('failed', self.turn('confirm'))
        self.assertEqual(self.balance(), 1)


    def test_crash_before_backend_submission_resumes_same_key(self):
        self.start()
        with patch.object(self.gateway, 'submit_transfer', side_effect=SystemExit()):
            with self.assertRaises(SystemExit):
                self.turn('confirm')
        key = self.task().submission_key
        self.assertEqual(self.task().status, 'SUBMITTING')
        recovered = self.new_manager()
        self.assertIn('successful', self.turn('resume', manager=recovered))
        self.assertEqual(self.task().submission_key, key)
        self.assertEqual(self.balance(), 4820.45)

    def test_confirm_after_second_start_requires_summary(self):
        self.start()
        self.start(amount='100')
        self.assertIn('Confirm transfer', self.turn('confirm'))
        self.assertEqual(self.balance(), 5320.45)

    def test_router_identifies_workflow_with_incomplete_slots(self):
        from bank_fast_path_router import Route, Slots
        from router import CapabilityRouter
        router = CapabilityRouter(client=Mock())
        proposal = Route(plan_id='transfer_money', slots=Slots(), fully_covered=False)
        with patch('router.route_request_with_proposal', return_value=(proposal, 0, proposal)):
            route = router.route('send some money to dingsijie@gmail.com')
        self.assertEqual(route.intent_type, IntentType.WORKFLOW)
        self.assertEqual(route.tools, ())

    def test_parser_structured_output_and_omissions(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = '{"action":"update","changes":{"amount":"300"}}'
        parser = TransferParser(client, 'mock')
        turn = parser.parse('make it 300')
        self.assertEqual(turn.changes.model_fields_set, {'amount'})
        client.chat.completions.create.return_value.choices[0].message.content = '{"action":"confirm","owner":"C10002"}'
        with self.assertRaises(ValueError):
            parser.parse('yes')

    def test_app_authentication_and_query_integration(self):
        agent = BankingAgent(self.store.path, 'app-conversation')
        self.addCleanup(agent.close)
        route = RouteResult(IntentType.WORKFLOW, 'transfer_money', 1)
        with patch.object(agent.router, 'route', return_value=route):
            self.assertIn('sign in', agent.handle('transfer 500 to dingsijie@gmail.com'))
            self.assertIsNone(agent.customer_context)
            self.assertTrue(agent.authenticate('4111111111111111', '1234'))
            with patch.object(agent.workflow.parser, 'parse', return_value=TransferTurn(
                action='start', changes=TransferDraft(amount='500', recipient_email='dingsijie@gmail.com'))):
                self.assertIn('Confirm transfer', agent.handle('transfer 500 to dingsijie@gmail.com'))
        with patch.object(agent.workflow.parser, 'parse', return_value=TransferTurn(action='other')), patch.object(
            agent.router, 'route', return_value=RouteResult(IntentType.COMMON, 'get_balance', 1)):
            self.assertIn('5,320.45', agent.handle('what is my balance?'))
        with patch.object(agent.workflow.parser, 'parse', return_value=TransferTurn(action='confirm')):
            self.assertIn('Confirm transfer', agent.handle('yes'))
        self.assertIn('retained', agent.reset())
        restarted = BankingAgent(self.store.path, 'app-conversation')
        self.addCleanup(restarted.close)
        self.assertIsNone(restarted.customer_context)
        with patch.object(restarted.router, 'route', return_value=route):
            self.assertIn('sign in', restarted.handle('resume transfer'))


if __name__ == '__main__':
    unittest.main()
