"""Module that provides the thread pool for :mod:`requests_toolbelt.threaded`.

This module provides three classes:

- :class:`~requests_toolbelt.threaded.pool.Pool`
- :class:`~requests_toolbelt.threaded.pool.ThreadResponse`
- :class:`~requests_toolbelt.threaded.pool.ThreadException`

In 98% of the situations you'll want to just use a
:class:`~requests_toolbelt.threaded.pool.Pool` and you'll treat a
:class:`~requests_toolbelt.threaded.pool.ThreadResponse` as if it were a
regular :class:`requests.Response`.

Here's an example:

.. code-block:: python

    # This example assumes Python 3
    import queue
    from requests_toolbelt.threaded import pool

    jobs = queue.Queue()
    urls = [
        # My list of URLs to get
    ]

    for url in urls:
        queue.put({'method': 'GET', 'url': url})

    p = pool.Pool(job_queue=q)
    p.join_all()

    for response in p.responses():
        print('GET {0}. Returned {1}.'.format(response.request_kwargs['url'],
                                              response.status_code))

This is clearly a bit underwhelming. This is why there's a short-cut class
method to create a :class:`~requests_toolbelt.threaded.pool.Pool` from a list
of URLs.

.. code-block:: python

    from requests_toolbelt.threaded import pool

    urls = [
        # My list of URLs to get
    ]

    p = pool.Pool.from_urls(urls)
    p.join_all()

    for response in p.responses():
        print('GET {0}. Returned {1}.'.format(response.request_kwargs['url'],
                                              response.status_code))

If one of the URLs in your list throws an exception, it will be accessible
from the :meth:`~Pool.exceptions` generator.

.. code-block:: python

    from requests_toolbelt.threaded import pool

    urls = [
        # My list of URLs to get
    ]

    p = pool.Pool.from_urls(urls)
    p.join_all()

    for exc in p.exceptions():
        print('GET {0}. Raised {1}.'.format(exc.request_kwargs['url'],
                                            exc.message))

"""
import multiprocessing
try:
    import queue  # Python 3
except ImportError:
    import Queue as queue

import requests

from . import thread


class Pool(object):
    """Pool that manages the threads containing sessions.

    .. todo:: Document format for a queue

    :param queue:
        The queue you're expected to use to which you should add items.
    :type queue: queue.Queue
    :param initializer:
        Function used to initialize an instance of ``session``.
    :type initializer: collections.Callable
    :param auth_generator:
        Function used to generate new auth credentials for the session.
    :type auth_generator: collections.Callable
    :param int num_threads:
        Number of threads to create.
    :param session:
    :type session: requests.Session
    """

    def __init__(self, job_queue, initializer=None, auth_generator=None,
                 num_processes=None, session=requests.Session):
        if num_processes is None:
            num_processes = multiprocessing.cpu_count() or 1

        if num_processes < 1:
            raise ValueError("Number of processes should at least be 1.")

        self._job_queue = job_queue
        self._response_queue = queue.Queue()
        self._exc_queue = queue.Queue()
        self._processes = num_processes
        self._initializer = initializer or _identity
        self._auth = auth_generator or _identity
        self._session = session
        self._pool = [
            thread.SessionThread(self._new_session(), self._job_queue,
                                 self._response_queue, self._exc_queue)
            for _ in range(self._processes)
        ]

    def _new_session(self):
        return self._auth(self._initializer(self._session()))

    @classmethod
    def from_exceptions(cls, exceptions, **kwargs):
        r"""Create a :class:`~Pool` from an :class:`~ThreadException`\ s.

        Provided an iterable that provides :class:`~ThreadException` objects,
        this classmethod will generate a new pool to retry the requests that
        caused the exceptions.

        :param exceptions:
            Iterable that returns :class:`~ThreadException`
        :type exceptions: iterable
        :param kwargs:
            Keyword arguments passed to the :class:`~Pool` initializer.
        :returns: An initialized :class:`~Pool` object.
        :rtype: :class:`~Pool`
        """
        job_queue = queue.Queue()
        for exc in exceptions:
            job_queue.put(exc.request_kwargs)

        return cls(job_queue=job_queue, **kwargs)

    @classmethod
    def from_urls(cls, urls, request_kwargs, **kwargs):
        """Create a :class:`~Pool` from an iterable of URLs.

        :param urls:
            Iterable that returns URLs with which we create a pool.
        :type urls: iterable
        :param dict request_kwargs:
            Dictionary of other keyword arguments to provide to the request
            method.
        :param kwargs:
            Keyword arguments passed to the :class:`~Pool` initializer.
        :returns: An initialized :class:`~Pool` object.
        :rtype: :class:`~Pool`
        """
        request_dict = {'method': 'GET'}
        request_dict.update(request_kwargs)
        job_queue = queue.Queue()
        for url in urls:
            job = request_dict.copy()
            job.update({'url': url})
            job_queue.put(job)

        return cls(job_queue=job_queue, **kwargs)

    def exceptions(self):
        """Iterate over all the exceptions in the pool.

        :returns: Generator of :class:`~ThreadException`
        """
        while True:
            exc = self.get_exception()
            if exc is None:
                break
            yield exc

    def get_exception(self):
        """Get an exception from the pool.

        :rtype: :class:`~ThreadException`
        """
        try:
            return ThreadException.from_queue(
                self._exc_queue.get_nowait()
            )
        except queue.Empty:
            return None

    def get_response(self):
        """Get a response from the pool.

        :rtype: :class:`~ThreadResponse`
        """
        try:
            return ThreadResponse.from_queue(
                self._response_queue.get_nowait()
            )
        except queue.Empty:
            return None

    def responses(self):
        """Iterate over all the responses in the pool.

        :returns: Generator of :class:`~ThreadResponse`
        """
        while True:
            resp = self.get_response()
            if resp is None:
                break
            yield resp

    def join_all(self):
        """Join all the threads to the master thread."""
        for session_thread in self._pool:
            session_thread.join()


class ThreadProxy(object):
    proxied_attr = None

    def __getattr__(self, attr):
        """Proxy attribute accesses to the proxied object."""
        get = object.__getattribute__
        if attr not in self.attrs:
            response = get(self, self.proxied_attr)
            return getattr(response, attr)
        else:
            return get(self, attr)

    @classmethod
    def from_queue(cls, qtuple):
        """Create an instance of ``cls`` from a queue result."""
        request_kwargs, proxied_obj = qtuple
        return cls(request_kwargs, proxied_obj)


class ThreadResponse(ThreadProxy):
    """A wrapper around a requests Response object.

    This will proxy most attribute access actions to the Response object. For
    example, if you wanted the parsed JSON from the response, you might do:

    .. code-block:: python

        thread_response = pool.get_response()
        json = thread_response.json()

    """
    proxied_attr = 'response'
    attrs = frozenset(['request_kwargs', 'response'])

    def __init__(self, request_kwargs, response):
        #: The original keyword arguments provided to the queue
        self.request_kwargs = request_kwargs
        #: The wrapped response
        self.response = response


class ThreadException(ThreadProxy):
    """A wrapper around an exception raised during a request.

    This will proxy most attribute access actions to the exception object. For
    example, if you wanted the message from the exception, you might do:

    .. code-block:: python

        thread_exc = pool.get_exception()
        msg = thread_exc.message

    """
    proxied_attr = 'exception'
    attrs = frozenset(['request_kwargs', 'exception'])

    def __init__(self, request_kwargs, exception):
        #: The original keyword arguments provided to the queue
        self.request_kwargs = request_kwargs
        #: The captured and wrapped exception
        self.exception = exception


def _identity(session_obj):
    return session_obj


__all__ = ['ThreadException', 'ThreadResponse', 'Pool']
