"""Offline actor-selection tests using an isolated in-memory database only."""
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

import r3_frontend_acceptance_actors as probe

Base = declarative_base()


def model(name, fields):
    return type(name, (Base,), {'__tablename__': name.lower(), 'id': Column(String, primary_key=True), **fields})


User = model('User', {key: Column(String) for key in ('email', 'platform_role', 'activation_status', 'verified_status')} | {'is_active': Column(Boolean), 'created_at': Column(Integer)})
Enterprise = model('Enterprise', {'verification_status': Column(String)})
Membership = model('Membership', {key: Column(String) for key in ('user_id', 'enterprise_id', 'role', 'status')} | {'created_at': Column(Integer)})
Product = model('Product', {key: Column(String) for key in ('name', 'product_type')})
Subscription = model('Subscription', {key: Column(String) for key in ('enterprise_id', 'product_id')} | {'created_at': Column(Integer)})
Order = model('Order', {key: Column(String) for key in ('buyer_enterprise_id', 'product_id', 'payment_status', 'buyer_user_id', 'order_no')})


class AccessDenied(Exception):
    pass


class ActorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.m = SimpleNamespace(User=User, Enterprise=Enterprise, Membership=Membership, Product=Product,
            SaaSSubscription=Subscription, Order=Order, HTTPException=AccessDenied,
            applications_access=lambda _db, _user, _sid: None)
        for uid, role in [('admin', 'super_admin'), ('operator', 'platform_operator'), ('provider', ''), ('member', '')]:
            self.db.add(User(id=uid, email=uid+'@example.invalid', platform_role=role,
                activation_status='active', verified_status='verified', is_active=True, created_at=1))
        self.db.add(Enterprise(id='enterprise', verification_status='verified'))
        for uid, role in [('provider', 'enterprise_admin'), ('member', 'member')]:
            self.db.add(Membership(id=uid, user_id=uid, enterprise_id='enterprise', role=role, status='active', created_at=1))
        self.db.add(Product(id='product', name='qa-r3-fe-owned', product_type='saas'))
        self.db.add(Subscription(id='subscription', enterprise_id='enterprise', product_id='product', created_at=1))
        self.db.add(Order(id='order', buyer_enterprise_id='enterprise', product_id='product', payment_status='paid', buyer_user_id='provider', order_no='qa-r3-fe-order'))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_existing_actors_and_non_owner_member_are_selected_without_row_updates(self):
        result = probe.choose(self.m, self.db)
        self.assertEqual(set(result), {'Admin', 'Operator', 'Provider', 'Member'})
        self.assertTrue(result['Member']['non_order_owner'])
        self.assertEqual(result['Member']['subscription_id'], 'subscription')
        self.assertFalse(self.db.dirty)
        self.assertFalse(self.db.new)
        self.assertFalse(self.db.deleted)

    def test_subscription_access_must_really_pass(self):
        def denied(*_args):
            raise AccessDenied()
        self.m.applications_access = denied
        self.assertNotIn('Member', probe.choose(self.m, self.db))

    def test_member_cannot_be_order_owner(self):
        self.db.get(Order, 'order').buyer_user_id = 'member'
        self.db.commit()
        self.assertNotIn('Member', probe.choose(self.m, self.db))

    def test_protected_order_and_product_are_excluded(self):
        for field, value in [('order_no', probe.PROTECTED_ORDER)]:
            setattr(self.db.get(Order, 'order'), field, value)
            self.db.commit()
            self.assertNotIn('Member', probe.choose(self.m, self.db))
        self.db.get(Order, 'order').order_no = 'qa-r3-fe-order'
        self.db.get(Product, 'product').name = probe.PROTECTED_PRODUCT
        self.db.commit()
        self.assertNotIn('Member', probe.choose(self.m, self.db))

    def test_disabled_or_unverified_members_are_not_selected(self):
        self.db.get(User, 'member').is_active = False
        self.db.commit()
        self.assertNotIn('Member', probe.choose(self.m, self.db))

    def test_private_files_are_0600_and_cannot_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory(prefix='qa-r3-fe-test-') as directory:
            path = Path(directory) / 'test.token'
            probe.private_write(path, 'not-a-real-credential')
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                probe.private_write(path, 'replacement')

    def test_password_transport_uses_environment_and_non_batch_mode_without_secret_argv(self):
        args = SimpleNamespace(ssh_target='root@192.168.10.10', ssh_key='/unused-key')
        command = probe.ssh_command(args, {'SSHPASS': 'test-value-not-a-real-password'})
        self.assertEqual(command[:3], ['sshpass', '-e', 'ssh'])
        self.assertIn('BatchMode=no', command)
        self.assertIn('PreferredAuthentications=password,keyboard-interactive', command)
        self.assertNotIn('test-value-not-a-real-password', command)
        self.assertNotIn('-p', command)
        self.assertNotIn('/unused-key', command)
        self.assertEqual(command[-1], args.ssh_target)

    def test_key_transport_retains_batch_mode_when_password_is_missing_or_empty(self):
        args = SimpleNamespace(ssh_target='root@192.168.10.10', ssh_key='/test-key')
        for environ in ({}, {'SSHPASS': ''}):
            command = probe.ssh_command(args, environ)
            self.assertEqual(command[0], 'ssh')
            self.assertNotIn('sshpass', command)
            self.assertIn('BatchMode=yes', command)
            self.assertIn('IdentitiesOnly=yes', command)
            self.assertEqual(command[-3:], ['-i', '/test-key', args.ssh_target])

    def test_default_transport_without_password_or_key_uses_existing_ssh_configuration(self):
        command = probe.ssh_command(SimpleNamespace(ssh_target='market', ssh_key=None), {})
        self.assertIn('BatchMode=yes', command)
        self.assertNotIn('-i', command)
        self.assertEqual(command[-1], 'market')


if __name__ == '__main__':
    unittest.main(verbosity=2)
