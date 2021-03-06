from __future__ import absolute_import, unicode_literals

import pytest

from datetime import datetime, timedelta

from case import Mock
from kombu import Exchange, Queue

from celery import uuid
from celery.app.amqp import Queues, utf8dict
from celery.five import keys
from celery.utils.time import to_utc


class test_TaskConsumer:

    def test_accept_content(self, app):
        with app.pool.acquire(block=True) as con:
            app.conf.accept_content = ['application/json']
            assert app.amqp.TaskConsumer(con).accept == {
                'application/json',
            }
            assert app.amqp.TaskConsumer(con, accept=['json']).accept == {
                'application/json',
            }


class test_ProducerPool:

    def test_setup_nolimit(self, app):
        app.conf.broker_pool_limit = None
        try:
            delattr(app, '_pool')
        except AttributeError:
            pass
        app.amqp._producer_pool = None
        pool = app.amqp.producer_pool
        assert pool.limit == app.pool.limit
        assert not pool._resource.queue

        r1 = pool.acquire()
        r2 = pool.acquire()
        r1.release()
        r2.release()
        r1 = pool.acquire()
        r2 = pool.acquire()

    def test_setup(self, app):
        app.conf.broker_pool_limit = 2
        try:
            delattr(app, '_pool')
        except AttributeError:
            pass
        app.amqp._producer_pool = None
        pool = app.amqp.producer_pool
        assert pool.limit == app.pool.limit
        assert pool._resource.queue

        p1 = r1 = pool.acquire()
        p2 = r2 = pool.acquire()
        r1.release()
        r2.release()
        r1 = pool.acquire()
        r2 = pool.acquire()
        assert p2 is r1
        assert p1 is r2
        r1.release()
        r2.release()


class test_Queues:

    def test_queues_format(self):
        self.app.amqp.queues._consume_from = {}
        assert self.app.amqp.queues.format() == ''

    def test_with_defaults(self):
        assert Queues(None) == {}

    def test_add(self):
        q = Queues()
        q.add('foo', exchange='ex', routing_key='rk')
        assert 'foo' in q
        assert isinstance(q['foo'], Queue)
        assert q['foo'].routing_key == 'rk'

    @pytest.mark.parametrize('ha_policy,qname,q,qargs,expected', [
        (None, 'xyz', 'xyz', None, None),
        (None, 'xyz', 'xyz', {'x-foo': 'bar'}, {'x-foo': 'bar'}),
        ('all', 'foo', Queue('foo'), None, {'x-ha-policy': 'all'}),
        ('all', 'xyx2',
         Queue('xyx2', queue_arguments={'x-foo': 'bari'}),
         None,
         {'x-ha-policy': 'all', 'x-foo': 'bari'}),
        (['A', 'B', 'C'], 'foo', Queue('foo'), None, {
            'x-ha-policy': 'nodes',
            'x-ha-policy-params': ['A', 'B', 'C']}),
    ])
    def test_with_ha_policy(self, ha_policy, qname, q, qargs, expected):
        queues = Queues(ha_policy=ha_policy, create_missing=False)
        queues.add(q, queue_arguments=qargs)
        assert queues[qname].queue_arguments == expected

    def test_select_add(self):
        q = Queues()
        q.select(['foo', 'bar'])
        q.select_add('baz')
        assert sorted(keys(q._consume_from)) == ['bar', 'baz', 'foo']

    def test_deselect(self):
        q = Queues()
        q.select(['foo', 'bar'])
        q.deselect('bar')
        assert sorted(keys(q._consume_from)) == ['foo']

    def test_with_ha_policy_compat(self):
        q = Queues(ha_policy='all')
        q.add('bar')
        assert q['bar'].queue_arguments == {'x-ha-policy': 'all'}

    def test_add_default_exchange(self):
        ex = Exchange('fff', 'fanout')
        q = Queues(default_exchange=ex)
        q.add(Queue('foo'))
        assert q['foo'].exchange.name == ''

    def test_alias(self):
        q = Queues()
        q.add(Queue('foo', alias='barfoo'))
        assert q['barfoo'] is q['foo']

    @pytest.mark.parametrize('queues_kwargs,qname,q,expected', [
        (dict(max_priority=10),
         'foo', 'foo', {'x-max-priority': 10}),
        (dict(max_priority=10),
         'xyz', Queue('xyz', queue_arguments={'x-max-priority': 3}),
         {'x-max-priority': 3}),
        (dict(max_priority=10),
         'moo', Queue('moo', queue_arguments=None),
         {'x-max-priority': 10}),
        (dict(ha_policy='all', max_priority=5),
         'bar', 'bar',
         {'x-ha-policy': 'all', 'x-max-priority': 5}),
        (dict(ha_policy='all', max_priority=5),
         'xyx2', Queue('xyx2', queue_arguments={'x-max-priority': 2}),
         {'x-ha-policy': 'all', 'x-max-priority': 2}),
        (dict(max_priority=None),
         'foo2', 'foo2',
         None),
        (dict(max_priority=None),
         'xyx3', Queue('xyx3', queue_arguments={'x-max-priority': 7}),
         {'x-max-priority': 7}),

    ])
    def test_with_max_priority(self, queues_kwargs, qname, q, expected):
        queues = Queues(**queues_kwargs)
        queues.add(q)
        assert queues[qname].queue_arguments == expected


class test_AMQP:

    def setup(self):
        self.simple_message = self.app.amqp.as_task_v2(
            uuid(), 'foo', create_sent_event=True,
        )

    def test_Queues__with_ha_policy(self):
        x = self.app.amqp.Queues({}, ha_policy='all')
        assert x.ha_policy == 'all'

    def test_Queues__with_max_priority(self):
        x = self.app.amqp.Queues({}, max_priority=23)
        assert x.max_priority == 23

    def test_send_task_message__no_kwargs(self):
        self.app.amqp.send_task_message(Mock(), 'foo', self.simple_message)

    def test_send_task_message__properties(self):
        prod = Mock(name='producer')
        self.app.amqp.send_task_message(
            prod, 'foo', self.simple_message, foo=1, retry=False,
        )
        assert prod.publish.call_args[1]['foo'] == 1

    def test_send_task_message__headers(self):
        prod = Mock(name='producer')
        self.app.amqp.send_task_message(
            prod, 'foo', self.simple_message, headers={'x1x': 'y2x'},
            retry=False,
        )
        assert prod.publish.call_args[1]['headers']['x1x'] == 'y2x'

    def test_send_task_message__queue_string(self):
        prod = Mock(name='producer')
        self.app.amqp.send_task_message(
            prod, 'foo', self.simple_message, queue='foo', retry=False,
        )
        kwargs = prod.publish.call_args[1]
        assert kwargs['routing_key'] == 'foo'
        assert kwargs['exchange'] == ''

    def test_send_event_exchange_string(self):
        evd = Mock(name='evd')
        self.app.amqp.send_task_message(
            Mock(), 'foo', self.simple_message, retry=False,
            exchange='xyz', routing_key='xyb',
            event_dispatcher=evd,
        )
        evd.publish.assert_called()
        event = evd.publish.call_args[0][1]
        assert event['routing_key'] == 'xyb'
        assert event['exchange'] == 'xyz'

    def test_send_task_message__with_delivery_mode(self):
        prod = Mock(name='producer')
        self.app.amqp.send_task_message(
            prod, 'foo', self.simple_message, delivery_mode=33, retry=False,
        )
        assert prod.publish.call_args[1]['delivery_mode'] == 33

    def test_routes(self):
        r1 = self.app.amqp.routes
        r2 = self.app.amqp.routes
        assert r1 is r2


class test_as_task_v2:

    def test_raises_if_args_is_not_tuple(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v2(uuid(), 'foo', args='123')

    def test_raises_if_kwargs_is_not_mapping(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v2(uuid(), 'foo', kwargs=(1, 2, 3))

    def test_countdown_to_eta(self):
        now = to_utc(datetime.utcnow()).astimezone(self.app.timezone)
        m = self.app.amqp.as_task_v2(
            uuid(), 'foo', countdown=10, now=now,
        )
        assert m.headers['eta'] == (now + timedelta(seconds=10)).isoformat()

    def test_expires_to_datetime(self):
        now = to_utc(datetime.utcnow()).astimezone(self.app.timezone)
        m = self.app.amqp.as_task_v2(
            uuid(), 'foo', expires=30, now=now,
        )
        assert m.headers['expires'] == (
            now + timedelta(seconds=30)).isoformat()

    def test_callbacks_errbacks_chord(self):

        @self.app.task
        def t(i):
            pass

        m = self.app.amqp.as_task_v2(
            uuid(), 'foo',
            callbacks=[t.s(1), t.s(2)],
            errbacks=[t.s(3), t.s(4)],
            chord=t.s(5),
        )
        _, _, embed = m.body
        assert embed['callbacks'] == [utf8dict(t.s(1)), utf8dict(t.s(2))]
        assert embed['errbacks'] == [utf8dict(t.s(3)), utf8dict(t.s(4))]
        assert embed['chord'] == utf8dict(t.s(5))
