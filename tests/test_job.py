# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from datetime import datetime
import time

from tests import fixtures, RQTestCase
from tests.helpers import strip_microseconds

from rq.compat import PY2, as_text, decode_redis_set
from rq.exceptions import NoSuchJobError, UnpickleError
from rq.job import Job, JobStatus
from rq.utils import utcformat
from rq.worker import Worker

try:
    from cPickle import loads, dumps
except ImportError:
    from pickle import loads, dumps


class TestJob(RQTestCase):
    def test_unicode(self):
        """Unicode in job description [issue405]"""
        job = Job.create(
            'myfunc',
            args=[12, "☃"],
            kwargs=dict(snowman="☃", null=None),
            connection=self.testconn,
        )

        if not PY2:
            # Python 3
            expected_string = "myfunc(12, '☃', null=None, snowman='☃')"
        else:
            # Python 2
            expected_string = u"myfunc(12, u'\\u2603', null=None, snowman=u'\\u2603')".decode('utf-8')

        self.assertEqual(
            job.description,
            expected_string,
        )

    def test_create_empty_job(self):
        """Creation of new empty jobs."""
        job = Job(connection=self.conn)

        # Jobs have a random UUID and a creation date
        self.assertIsNotNone(job.id)
        self.assertIsNotNone(job.created_at)

        # ...and nothing else
        self.assertIsNone(job.origin)
        self.assertIsNone(job.enqueued_at)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.ended_at)
        self.assertIsNone(job.result)
        self.assertIsNone(job.exc_info)

        with self.assertRaises(ValueError):
            job.func
        with self.assertRaises(ValueError):
            job.instance
        with self.assertRaises(ValueError):
            job.args
        with self.assertRaises(ValueError):
            job.kwargs

    def test_create_typical_job(self):
        """Creation of jobs for function calls."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4),
                         kwargs=dict(z=2), connection=self.testconn,)

        # Jobs have a random UUID
        self.assertIsNotNone(job.id)
        self.assertIsNotNone(job.created_at)
        self.assertIsNotNone(job.description)
        self.assertIsNotNone(job.origin)
        self.assertIsNone(job.instance)

        # Job data is set...
        self.assertEqual(job.func, fixtures.some_calculation)
        self.assertEqual(job.args, (3, 4))
        self.assertEqual(job.kwargs, {'z': 2})

        # ...but metadata is not
        self.assertIsNone(job.enqueued_at)
        self.assertIsNone(job.result)

    def test_create_instance_method_job(self):
        """Creation of jobs for instance methods."""
        n = fixtures.Number(2)
        job = Job.create(func=n.div, args=(4,), connection=self.testconn)

        # Job data is set
        self.assertEqual(job.func, n.div)
        self.assertEqual(job.instance, n)
        self.assertEqual(job.args, (4,))

    def test_create_job_from_string_function(self):
        """Creation of jobs using string specifier."""
        job = Job.create(func='tests.fixtures.say_hello', args=('World',),
                         connection=self.testconn,)

        # Job data is set
        self.assertEqual(job.func, fixtures.say_hello)
        self.assertIsNone(job.instance)
        self.assertEqual(job.args, ('World',))

    def test_create_job_from_callable_class(self):
        """Creation of jobs using a callable class specifier."""
        kallable = fixtures.CallableObject()
        job = Job.create(func=kallable, connection=self.testconn)

        self.assertEqual(job.func, kallable.__call__)
        self.assertEqual(job.instance, kallable)

    def test_job_properties_set_data_property(self):
        """Data property gets derived from the job tuple."""
        job = Job(connection=self.conn)
        job.func_name = 'foo'
        fname, instance, args, kwargs = loads(job.data)

        self.assertEqual(fname, job.func_name)
        self.assertEqual(instance, None)
        self.assertEqual(args, ())
        self.assertEqual(kwargs, {})

    def test_data_property_sets_job_properties(self):
        """Job tuple gets derived lazily from data property."""
        job = Job(connection=self.conn)
        job.data = dumps(('foo', None, (1, 2, 3), {'bar': 'qux'}))

        self.assertEqual(job.func_name, 'foo')
        self.assertEqual(job.instance, None)
        self.assertEqual(job.args, (1, 2, 3))
        self.assertEqual(job.kwargs, {'bar': 'qux'})

    def test_save(self):  # noqa
        """Storing jobs."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4),
                         kwargs=dict(z=2), connection=self.testconn)

        # Saving creates a Redis hash
        self.assertEqual(self.testconn.exists(job.key), False)
        job.save()
        self.assertEqual(self.testconn.type(job.key), b'hash')

        # Saving writes pickled job data
        unpickled_data = loads(self.testconn.hget(job.key, 'data'))
        self.assertEqual(unpickled_data[0], 'tests.fixtures.some_calculation')

    def test_fetch(self):
        """Fetching jobs."""
        # Prepare test
        self.testconn.hset('rq:job:some_id', 'data',
                           "(S'tests.fixtures.some_calculation'\nN(I3\nI4\nt(dp1\nS'z'\nI2\nstp2\n.")
        self.testconn.hset('rq:job:some_id', 'created_at',
                           '2012-02-07T22:13:24Z')

        # Fetch returns a job
        job = self.conn.get_job('some_id')
        self.assertEqual(job.id, 'some_id')
        self.assertEqual(job.func_name, 'tests.fixtures.some_calculation')
        self.assertIsNone(job.instance)
        self.assertEqual(job.args, (3, 4))
        self.assertEqual(job.kwargs, dict(z=2))
        self.assertEqual(job.created_at, datetime(2012, 2, 7, 22, 13, 24))

    def test_persistence_of_empty_jobs(self):  # noqa
        """Storing empty jobs."""
        job = Job(connection=self.conn)
        with self.assertRaises(ValueError):
            job.save()

    def test_persistence_of_typical_jobs(self):
        """Storing typical jobs."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4),
                         kwargs=dict(z=2), connection=self.testconn)
        job.save()

        expected_date = strip_microseconds(job.created_at)
        stored_date = self.testconn.hget(job.key, 'created_at').decode('utf-8')
        self.assertEqual(
            stored_date,
            utcformat(expected_date))

        # ... and no other keys are stored
        self.assertEqual(
            sorted(self.testconn.hkeys(job.key)),
            [b'created_at', b'data', b'description', b'origin'])

    def test_persistence_of_parent_job(self):
        """Storing jobs with parent job, either instance or key."""
        parent_job = Job.create(func=fixtures.some_calculation,
                                connection=self.testconn)
        parent_job.save()
        job = Job.create(func=fixtures.some_calculation, depends_on=parent_job,
                         connection=self.testconn,)
        job.save()
        stored_job = self.conn.get_job(job.id)
        self.assertEqual(stored_job.parent_ids[0], parent_job.id)

        job = Job.create(func=fixtures.some_calculation,
                         depends_on=parent_job.id, connection=self.testconn)
        job.save()
        stored_job = self.conn.get_job(job.id)
        self.assertEqual(stored_job.parent_ids[0], parent_job.id)

    def test_store_then_fetch(self):
        """Store, then fetch."""
        job = Job.create(func=fixtures.some_calculation, args=(3, 4),
                         kwargs=dict(z=2), connection=self.testconn)
        job.save()

        job2 = self.conn.get_job(job.id)
        self.assertEqual(job.func, job2.func)
        self.assertEqual(job.args, job2.args)
        self.assertEqual(job.kwargs, job2.kwargs)

        # Mathematical equation
        self.assertEqual(job, job2)

    def test_fetching_can_fail(self):
        """Fetching fails for non-existing jobs."""
        with self.assertRaises(NoSuchJobError):
            self.conn.get_job('b4a44d44-da16-4620-90a6-798e8cd72ca0')

    def test_fetching_unreadable_data(self):
        """Fetching succeeds on unreadable data, but lazy props fail."""
        # Set up
        job = Job.create(func=fixtures.some_calculation, args=(3, 4),
                         kwargs=dict(z=2), connection=self.testconn)
        job.save()

        # Just replace the data hkey with some random noise
        self.testconn.hset(job.key, 'data', 'this is no pickle string')
        job.refresh()

        for attr in ('func_name', 'instance', 'args', 'kwargs'):
            with self.assertRaises(UnpickleError):
                getattr(job, attr)

    def test_job_is_unimportable(self):
        """Jobs that cannot be imported throw exception on access."""
        job = Job.create(func=fixtures.say_hello, args=('Lionel',),
                         connection=self.testconn)
        job.save()

        # Now slightly modify the job to make it unimportable (this is
        # equivalent to a worker not having the most up-to-date source code
        # and unable to import the function)
        data = self.testconn.hget(job.key, 'data')
        unimportable_data = data.replace(b'say_hello', b'nay_hello')
        self.testconn.hset(job.key, 'data', unimportable_data)

        job.refresh()
        with self.assertRaises(AttributeError):
            job.func  # accessing the func property should fail

    def test_custom_meta_is_persisted(self):
        """Additional meta data on jobs are stored persisted correctly."""
        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         args=('Lionel',))
        job.meta['foo'] = 'bar'
        job.save()

        raw_data = self.testconn.hget(job.key, 'meta')
        self.assertEqual(loads(raw_data)['foo'], 'bar')

        job2 = self.conn.get_job(job.id)
        self.assertEqual(job2.meta['foo'], 'bar')

    def test_result_ttl_is_persisted(self):
        """Ensure that job's result_ttl is set properly"""
        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         args=('Lionel',), result_ttl=10)
        job.save()
        self.conn.get_job(job.id)
        self.assertEqual(job.result_ttl, 10)

        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         args=('Lionel',))
        job.save()
        self.conn.get_job(job.id)
        self.assertEqual(job.result_ttl, None)

    def test_description_is_persisted(self):
        """Ensure that job's custom description is set properly"""
        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         args=('Lionel',), description='Say hello!')
        job.save()
        self.conn.get_job(job.id)
        self.assertEqual(job.description, 'Say hello!')

        # Ensure job description is constructed from function call string
        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         args=('Lionel',))
        job.save()
        self.conn.get_job(job.id)
        if PY2:
            self.assertEqual(job.description, "tests.fixtures.say_hello(u'Lionel')")
        else:
            self.assertEqual(job.description, "tests.fixtures.say_hello('Lionel')")

    def test_job_async_status_finished(self):
        queue = self.conn.mkqueue(async=False)
        job = queue.enqueue(fixtures.say_hello)
        self.assertEqual(job.result, 'Hi there, Stranger!')
        self.assertEqual(job.get_status(), JobStatus.FINISHED)

    def test_get_result_ttl(self):
        """Getting job result TTL."""
        job_result_ttl = 1
        default_ttl = 2
        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         result_ttl=job_result_ttl)
        job.save()
        self.assertEqual(job.get_result_ttl(default_ttl=default_ttl), job_result_ttl)
        self.assertEqual(job.get_result_ttl(), job_result_ttl)
        job = Job.create(connection=self.testconn, func=fixtures.say_hello)
        job.save()
        self.assertEqual(job.get_result_ttl(default_ttl=default_ttl), default_ttl)
        self.assertEqual(job.get_result_ttl(), None)

    def test_get_job_ttl(self):
        """Getting job TTL."""
        ttl = 1
        job = Job.create(connection=self.testconn, func=fixtures.say_hello,
                         ttl=ttl)
        job.save()
        self.assertEqual(job.get_ttl(), ttl)
        job = Job.create(connection=self.testconn, func=fixtures.say_hello)
        job.save()
        self.assertEqual(job.get_ttl(), None)

    def test_ttl_via_enqueue(self):
        ttl = 1
        queue = self.conn.mkqueue()
        job = queue.enqueue(fixtures.say_hello, ttl=ttl)
        self.assertEqual(job.get_ttl(), ttl)

    def test_never_expire_during_execution(self):
        """Test what happens when job expires during execution"""
        ttl = 1
        queue = self.conn.mkqueue()
        job = queue.enqueue(fixtures.long_running_job, args=(2,), ttl=ttl)
        self.assertEqual(job.get_ttl(), ttl)
        job.save()
        job.perform()
        self.assertEqual(job.get_ttl(), -1)
        self.assertTrue(self.conn.job_exists(job.id))
        self.assertEqual(job.result, 'Done sleeping...')

    def test_cleanup(self):
        """Test that jobs and results are expired properly."""
        job = Job.create(connection=self.testconn, func=fixtures.say_hello)
        job.save()

        # Jobs with negative TTLs don't expire
        job.cleanup(ttl=-1)
        self.assertEqual(self.testconn.ttl(job.key), -1)

        # Jobs with positive TTLs are eventually deleted
        job.cleanup(ttl=100)
        self.assertEqual(self.testconn.ttl(job.key), 100)

        # Jobs with 0 TTL are immediately deleted
        job.cleanup(ttl=0)
        with self.assertRaises(NoSuchJobError):
            self.conn.get_job(job.id)

    def test_register_dependency(self):
        """Ensure dependency registration works properly."""
        registry = self.conn.get_deferred_registry('some_queue')
        j1 = Job.create(fixtures.say_hello, connection=self.testconn).save()
        j2 = Job.create(fixtures.say_hello, connection=self.testconn).save()
        parent_jobs = [j1, j2]
        parent_ids = [j.id for j in parent_jobs]

        queue = self.conn.mkqueue('some_queue')
        job = queue.enqueue(fixtures.say_hello,
                            depends_on=[parent_jobs[0], parent_ids[1]])

        for parent in parent_jobs:
            self.assertEqual(
                decode_redis_set(self.testconn.smembers(parent.children_key)),
                set([job.id])
            )
        self.assertEqual(registry.get_job_ids(), [job.id])

        job.delete()

        self.assertFalse(self.testconn.exists(job.key))
        self.assertFalse(self.testconn.exists(job.children_key))

    def test_delete(self):
        """job.delete() deletes itself & dependents mapping from Redis."""
        queue = self.conn.mkqueue()
        job = queue.enqueue(fixtures.say_hello)

        job2 = Job.create(fixtures.say_hello, depends_on=job,
                          connection=self.testconn)
        job2._enqueue_or_deferr()
        job.delete()
        self.assertFalse(self.testconn.exists(job.key))
        self.assertFalse(self.testconn.exists(job.children_key))

        self.assertNotIn(job.id, queue.get_job_ids())

    def test_get_call_string_unicode(self):
        """test call string with unicode keyword arguments"""
        queue = self.conn.mkqueue()

        job = queue.enqueue(fixtures.echo,
                            arg_with_unicode=fixtures.UnicodeStringObject())
        self.assertIsNotNone(job.get_call_string())
        job.perform()

    def test_create_job_with_ttl_should_have_ttl_after_enqueued(self):
        """
        test creating jobs with ttl and checks if get_jobs returns it
        properly [issue502]
        """
        queue = self.conn.mkqueue()
        queue.enqueue(fixtures.say_hello, name="1234", ttl=10)
        job = queue.get_jobs()[0]
        self.assertEqual(job.ttl, 10)

    def test_create_job_with_ttl_should_expire(self):
        """test if a job created with ttl expires [issue502]"""
        queue = self.conn.mkqueue()
        queue.enqueue(fixtures.say_hello, name="1234", ttl=1)
        time.sleep(1)
        self.assertEqual(0, len(queue.get_jobs()))

    def test_future_result_adds_dependency(self):
        """Enqueing jobs with _FutureResult arguments should add to the
        depends_on field"""
        queue = self.conn.mkqueue()
        step_1 = queue.enqueue(fixtures.fibonacci_step, two_back=0, one_back=1)
        step_2 = queue.enqueue(fixtures.fibonacci_step, two_back=1,
                               one_back=step_1.future_result)

        self.assertEqual(
            decode_redis_set(self.testconn.smembers(step_1.children_key)),
            set([step_2.id])
        )
        self.assertEqual(step_2.parent_ids, [step_1.id])

    def test_future_result_adds_dependency_from_list(self):
        """Enqueing jobs with _FutureResult in a container argument should still
        add to the depends_on field"""
        queue = self.conn.mkqueue()
        step_1 = queue.enqueue(fixtures.n_back_sum, [])
        step_2 = queue.enqueue(fixtures.n_back_sum, [step_1.future_result])
        step_3 = queue.enqueue(fixtures.n_back_sum, [step_1.future_result,
                                                     step_2.future_result])

        self.assertEqual(
            decode_redis_set(self.testconn.smembers(step_1.children_key)),
            set([step_2.id, step_3.id])
        )
        self.assertEqual(step_2.parent_ids, [step_1.id])
        self.assertEqual(step_3.parent_ids, [step_1.id, step_2.id])

    def test_future_result_resolves_when_performed(self):
        """ Future results get filled in when a job is performed """
        queue = self.conn.mkqueue()
        step_1 = queue.enqueue(fixtures.fibonacci_step, two_back=0, one_back=1)
        step_2 = queue.enqueue(fixtures.fibonacci_step, two_back=1,
                               one_back=step_1.future_result)

        result_1 = step_1.perform()
        result_2 = step_2.perform()

        self.assertEqual(result_1, 1)
        self.assertEqual(result_2, 2)

