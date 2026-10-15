import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from test_support import BankServiceFixture
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
        self.service = BankServiceFixture(self.tmp.name)
        self.addCleanup(self.service.close)
        self.gateway = self.service.gateway()
        self.addCleanup(self.gateway.close)
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

    def test_close_cancels_unfinished_task(self):
        self.start()
        self.manager.close()  # conversation end
        self.manager = self.new_manager()
        self.assertEqual(self.task().status, "CANCELLED")

    def test_complete_confirm_duplicate(self):
        self.assertIn('Confirm transfer', self.start())
        self.assertEqual(self.balance(), 5320.45)
        self.assertIn('successful', self.turn('confirm'))
        self.turn('confirm')
        self.assertEqual(self.balance(), 4820.45)
        self.assertEqual(len([tx for tx in self.gateway.get_transactions('CHK-001')['transactions'] if 'transaction_id' in tx]), 1)
        self.assertIn('Transfer to', self.gateway.get_transactions('CHK-001')['transactions'][0]['description'])

    def test_confirmation_shows_computed_fee(self):
        self.assertIn('Mock fee: CAD 5.00.', self.start())  # 1% of 500
        self.assertIn('Mock fee: CAD 2.00.', self.turn('update', {'amount': '200'}))  # 1% of 200

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
        reloaded = self.service.gateway()
        self.addCleanup(reloaded.close)
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
        self.assertEqual(len([tx for tx in self.gateway.get_transactions('CHK-001')['transactions'] if 'transaction_id' in tx]), 1)

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
        with self.service.repository.connect() as db:
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
        agent = BankingAgent(self.store.path, 'app-conversation', self.service.url)
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
        restarted = BankingAgent(self.store.path, 'app-conversation', self.service.url)
        self.addCleanup(restarted.close)
        self.assertIsNone(restarted.customer_context)
        with patch.object(restarted.router, 'route', return_value=route):
            self.assertIn('sign in', restarted.handle('resume transfer'))

    def test_message_after_finished_transfer_goes_to_router(self):
        agent = BankingAgent(self.store.path, 'post-transfer-conversation', self.service.url)
        self.addCleanup(agent.close)
        with patch.object(agent.router, 'route', return_value=RouteResult(IntentType.WORKFLOW, 'transfer_money', 1)):
            self.assertIn('sign in', agent.handle('transfer 500 to dingsijie@gmail.com'))
            self.assertTrue(agent.authenticate('4111111111111111', '1234'))
            with patch.object(agent.workflow.parser, 'parse', return_value=TransferTurn(
                    action='start', changes=TransferDraft(amount='500', recipient_email='dingsijie@gmail.com'))):
                self.assertIn('Confirm transfer', agent.handle('transfer 500 to dingsijie@gmail.com'))
            # Complete it so the stored task is terminal.
            with patch.object(agent.workflow.parser, 'parse', return_value=TransferTurn(action='confirm')):
                self.assertIn('Transfer successful', agent.handle('yes'))
        # A normal question must now reach the router, not the transfer workflow.
        with patch.object(agent.common_handler, 'handle', return_value='CONTACT_ANSWER'), \
             patch.object(agent.router, 'route', return_value=RouteResult(IntentType.COMMON, 'contact_information', 1)):
            self.assertEqual(agent.handle('Tell me your open hours'), 'CONTACT_ANSWER')


class DeterministicSlotTests(unittest.TestCase):
    def _parser(self, model_json):
        from unittest.mock import MagicMock
        from workflows.parser import TransferParser
        client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content=model_json))]
        client.chat.completions.create.return_value = resp
        return TransferParser(client, "model")

    def test_bare_yes_forces_confirm_only_when_awaiting(self):
        from workflows.models import TransferTask
        awaiting = TransferTask(owner="C10001", conversation_id="c",
                                draft=TransferDraft(amount="100", recipient_email="dingsijie@gmail.com",
                                                    source_account_id="CHK-001"),
                                status="AWAITING_CONFIRMATION", confirmation_session="s")
        # Model misreads a bare "Yes" as a no-op update; we still confirm.
        self.assertEqual(self._parser('{"action":"update","changes":{}}').parse("Yes", awaiting).action, "confirm")
        collecting = TransferTask(owner="C10001", conversation_id="c",
                                  draft=TransferDraft(amount="100"), status="COLLECTING")
        self.assertEqual(self._parser('{"action":"update","changes":{}}').parse("Yes", collecting).action, "update")

    def test_new_transfer_forced_after_terminal_task(self):
        from decimal import Decimal
        from workflows.models import TransferTask
        done = TransferTask(owner="C10001", conversation_id="c",
                            draft=TransferDraft(amount="100", recipient_email="dingsijie@gmail.com",
                                                source_account_id="CHK-001"),
                            status="SUCCEEDED")
        # Model is unsure (returns "other"); a fresh transfer request still starts.
        turn = self._parser('{"action":"other","changes":{}}').parse(
            "I want to transfer 100 dollar to the email dingsijie@gmail.com", done)
        self.assertEqual(turn.action, "start")
        self.assertEqual(turn.changes.amount, Decimal("100"))
        self.assertEqual(turn.changes.recipient_email, "dingsijie@gmail.com")
        # An unrelated question after a done transfer must not start a new one.
        self.assertEqual(self._parser('{"action":"other","changes":{}}').parse(
            "what is my balance?", done).action, "other")

    def test_email_and_amount_extracted_verbatim(self):
        from workflows.parser import _deterministic_slots
        slots, added = _deterministic_slots(
            'I want to transfer 100 dollar to the email dingsijie@gmail.com', {})
        self.assertTrue(added)
        self.assertEqual(slots['recipient_email'], 'dingsijie@gmail.com')
        self.assertEqual(slots['amount'], '100')

    def test_bare_email_becomes_update_not_clarify(self):
        from workflows.parser import _deterministic_slots
        slots, added = _deterministic_slots('dingsijie@gmail.com', {})
        self.assertTrue(added)
        self.assertEqual(slots['recipient_email'], 'dingsijie@gmail.com')
        self.assertNotIn('amount', slots)

    def test_amount_ignores_account_id_digits(self):
        from workflows.parser import _deterministic_slots
        slots, added = _deterministic_slots('send 250 to CHK-001', {})
        self.assertEqual(slots.get('amount'), '250')

    def test_model_amount_wins_and_no_number_means_no_fill(self):
        from workflows.parser import _deterministic_slots
        slots, _ = _deterministic_slots('please pause', {'amount': '500'})
        self.assertEqual(slots['amount'], '500')
        slots, added = _deterministic_slots('please pause', {})
        self.assertFalse(added)
        self.assertNotIn('amount', slots)


    def test_bare_control_words_and_contact_skip_model(self):
        from workflows.models import TransferTask
        task = TransferTask(owner="C10001", conversation_id="c", status="AWAITING_CONFIRMATION")
        for msg, want in [("pause", "pause"), ("Cancel the transfer", "cancel"),
                          ("resume transfer", "resume"),
                          ("Can I go to your office to do such transfer now?", "other"),
                          ("When do you open?", "other")]:
            parser = self._parser('{"action":"other","changes":{}}')
            self.assertEqual(parser.parse(msg, task).action, want)
            parser.client.chat.completions.create.assert_not_called()
        # An email-bearing message is a workflow turn, not a contact question.
        turn = self._parser('{"action":"update","changes":{"recipient_email":"a@b.com"}}').parse(
            "send to a@b.com office", task)
        self.assertEqual(turn.action, "update")


if __name__ == '__main__':
    unittest.main()
