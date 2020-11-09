from __future__ import print_function

import queue
from collections import deque
from concurrent.futures.thread import ThreadPoolExecutor
from contextlib import closing, contextmanager
from threading import Condition, Thread, current_thread
from time import sleep, time

start = time()
END = object()


def log(template, *args):
    msg = template.format(*args)
    name = current_thread().name
    print("{:>20} {:5.2f} {}\n".format(name, time() - start, msg), end="")


@contextmanager
def auto_cancel(future):
    try:
        yield future
    except BaseException:
        future.cancel()
        raise


def pipe_coroutine(input_queue, coro_func, *output_queues, forward_end=True):
    while True:
        if not input_queue:
            yield
        else:
            item = input_queue.popleft()
            if coro_func is None or item is END and forward_end:
                for output_queue in output_queues:
                    output_queue.append(item)
            if item is END:
                break
            if coro_func is not None:
                with closing(coro_func(item)) as coroutine:
                    for res in coroutine:
                        if res is not None:
                            for output_queue in output_queues:
                                output_queue.append(res)
                        else:
                            yield


class ParallelCoroutineGroup(object):
    def __init__(self, add_queue=None, at_end=None):
        self.add_queue = deque() if add_queue is None else add_queue
        self._at_end = at_end
        self._complete = False
        self._coroutines = []

    @classmethod
    def static(cls, *coroutines, complete=True):
        add_queue = deque(coroutines)
        if complete:
            add_queue.append(END)
        return cls(add_queue)

    @property
    def finished(self):
        return self._complete and not self._coroutines

    def coroutine(self):
        try:
            while not self.finished:
                self._load_added()
                finished = []
                for coroutine in self._coroutines:
                    try:
                        res = next(coroutine)
                        assert res is None
                    except StopIteration:
                        finished.append(coroutine)
                for coroutine in finished:
                    self._coroutines.remove(coroutine)
                yield
        except BaseException:
            for coroutine in self._coroutines:
                coroutine.close()
            raise
        if self._at_end:
            self._at_end()

    def _load_added(self):
        while self.add_queue:
            added = self.add_queue.popleft()
            if added is END:
                self._complete = True
            else:
                self._coroutines.append(iter(added))


class LimitingExecutorQueue(object):
    def __init__(self, max_open):
        self._max_open = max_open
        self._active_open = 0
        self._none_queue = deque()
        self._open_queue = deque()
        self._close_queue = deque()
        self._condition = Condition()

    def put(self, work_item):
        if work_item is None:
            self._none_queue.append(work_item)
        elif hasattr(work_item.fn, "opener_mark"):
            self._open_queue.append(work_item)
        elif hasattr(work_item.fn, "closer_mark"):
            self._close_queue.append(work_item)
        else:
            raise RuntimeError("Unsupported call")
        with self._condition:
            self._condition.notify()

    def get(self, block=True):
        with self._condition:
            while not self._has_allowed_work():
                if block:
                    self._condition.wait()
                else:
                    raise queue.Empty
            if self._close_queue:
                self._active_open -= 1
                return self._close_queue.popleft()
            elif self._none_queue:
                return self._none_queue.popleft()
            else:
                self._active_open += 1
                return self._open_queue.popleft()

    def get_nowait(self):
        return self.get(block=False)

    @staticmethod
    def opener(method):
        method.opener_mark = None
        return method

    @staticmethod
    def closer(method):
        method.closer_mark = None
        return method

    def _has_allowed_work(self):
        return (
            self._none_queue
            or self._close_queue
            or self._open_queue
            and self._active_open < self._max_open
        )


class SessionService(ThreadPoolExecutor):
    def __init__(self, max_sessions):
        super(SessionService, self).__init__(max_sessions, self.__class__.__name__)
        self._work_queue = LimitingExecutorQueue(max_sessions)

    def open_session(self, name):
        @LimitingExecutorQueue.opener
        def do_open():
            log("Opening session: {}", name)
            sleep(2)
            log("Session opened: {}", name)
            return SessionService.Session(name, self)

        log("Scheduling session open {}", name)
        return self.submit(do_open)

    def close_session(self, session):
        @LimitingExecutorQueue.closer
        def do_close():
            log("Closing session: {}", session)
            sleep(1)
            log("Session closed: {}", session)

        log("Scheduling session close {}", session)
        return self.submit(do_close)

    class Session(object):
        def __init__(self, name, session_service):
            self.name = name
            self._sesson_service = session_service

        def __str__(self):
            return "Session " + self.name

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self._sesson_service.close_session(self.name)


class RenderingService(ThreadPoolExecutor):
    def __init__(self, parallelism):
        super(RenderingService, self).__init__(parallelism, self.__class__.__name__)

    def render(self, dom):
        def do_render():
            log("Rendering: {}", dom)
            sleep(2)
            log("Done rendering: {}", dom)
            return "Rendered " + dom

        log("Scheduling render {}", dom)
        return self.submit(do_render)


class CheckService(ThreadPoolExecutor):
    def __init__(self, parallelism):
        super(CheckService, self).__init__(parallelism, self.__class__.__name__)

    def check(self, render_result):
        def do_check():
            log("Checking: {}", render_result)
            sleep(1)
            log("Done Checking: {}", render_result)
            return "Checked " + render_result

        log("Scheduling check {}", render_result)
        return self.submit(do_check)


class CollectionService(ThreadPoolExecutor):
    def __init__(self, parallelism):
        super(CollectionService, self).__init__(parallelism, self.__class__.__name__)

    def collect_resources(self, dom):
        def do_collect():
            log("Collecting resources: {}", dom)
            sleep(3)
            log("Done collecting resources: {}", dom)
            return dom + " with resources"

        log("Scheduling resource collection {}", dom)
        return self.submit(do_collect)


class VGTestSession:
    def __init__(self, name, browsers_list, results):
        self._name = name
        self.tests = [VGTest(b, results) for b in browsers_list]
        self._session = None
        self._checks_queue = deque()
        resource_collection_group = ParallelCoroutineGroup(
            at_end=self._send_resources_collected
        )
        self._coroutines = ParallelCoroutineGroup.static(
            self._establish_connection(),
            resource_collection_group.coroutine(),
            pipe_coroutine(
                self._checks_queue,
                self._create_collect_resources_task,
                resource_collection_group.add_queue,
            ),
            complete=False,
        )

    def add_check(self, check):
        log("Check {} defined", check)
        self._checks_queue.append(check)

    def complete(self):
        log("Test {} defined", self._name)
        self._checks_queue.append(END)

    def coroutine(self):
        coroutine = self._coroutines.coroutine()
        for _ in coroutine:
            if self._session:
                break
            else:
                yield
        with self._session:
            self._coroutines.add_queue.extend(test.coroutine() for test in self.tests)
            self._coroutines.add_queue.append(END)
            for _ in coroutine:
                yield

    def _establish_connection(self):
        session_future = session_service.open_session(self._name)
        with auto_cancel(session_future):
            while not session_future.done():
                yield
            self._session = session_future.result()

    def _create_collect_resources_task(self, dom):
        yield self._collect_resources(dom)

    def _collect_resources(self, dom):
        collected_future = resource_collection_service.collect_resources(dom)
        with auto_cancel(collected_future):
            while not collected_future.done():
                yield
            result = collected_future.result()
            for test in self.tests:
                test.input_queue.append(result)

    def _send_resources_collected(self):
        for test in self.tests:
            test.input_queue.append(END)


class VGTest:
    def __init__(self, browser_info, results):
        self._browser_info = browser_info
        self.input_queue = deque()
        render_results = deque()
        self._coroutines = ParallelCoroutineGroup.static(
            pipe_coroutine(self.input_queue, self._render, render_results),
            pipe_coroutine(render_results, self._check, results, forward_end=False),
        )

    def coroutine(self):
        return self._coroutines.coroutine()

    def _render(self, dom_with_resourcs):
        render_request = "{} in {}".format(dom_with_resourcs, self._browser_info)
        render_future = rendering_service.render(render_request)
        with auto_cancel(render_future):
            while not render_future.done():
                yield
            yield render_future.result()

    def _check(self, render_result):
        check_future = check_service.check(render_result)
        with auto_cancel(check_future):
            while not check_future.done():
                yield
            yield check_future.result()


class VGRunner(object):
    def __init__(self):
        self.sessions = []
        self.results = []
        self._thread = Thread(target=self._run, name=self.__class__.__name__)
        self._coroutines = ParallelCoroutineGroup()
        self._canceled = False

    def add_session(self, name, browsers):
        if self.sessions:
            self.sessions[-1].complete()
        self.sessions.append(VGTestSession(name, browsers, self.results))
        self._coroutines.add_queue.append(self.sessions[-1].coroutine())
        return self.sessions[-1]

    def wait(self):
        if self._thread.is_alive():
            self.complete()
            self._thread.join()

    def complete(self):
        if self.sessions:
            self.sessions[-1].complete()
        self._coroutines.add_queue.append(END)

    def cancel(self):
        self._canceled = True
        if self._thread.is_alive():
            self._thread.join()

    def _run(self):
        coroutine = self._coroutines.coroutine()
        for _ in coroutine:
            if self._canceled:
                coroutine.close()
            sleep(0.5)
        log("All is done, canceled: {}", self._canceled)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._thread.is_alive():
            self.cancel()


session_service = SessionService(2)
resource_collection_service = CollectionService(10)
rendering_service = RenderingService(10)
check_service = CheckService(10)
runner = VGRunner()

try:
    with resource_collection_service, rendering_service, check_service, runner:
        test = runner.add_session("a", ["chrome", "safari"])
        sleep(0.5)
        test.add_check("a_1")
        sleep(0.5)
        test.add_check("a_2")

        test = runner.add_session("b", ["chrome", "firefox"])
        sleep(0.5)
        test.add_check("b_1")
        sleep(0.5)
        test.add_check("b_2")

        test = runner.add_session("c", ["chrome"])
        sleep(0.5)
        test.add_check("c_1")
        sleep(0.5)
        test.add_check("c_2")
        sleep(0.5)
        test.add_check("c_3")

        test = runner.add_session("d", ["chrome", "firefox"])
        test.add_check("d_1")

        log("All defined")
        runner.wait()

finally:
    print(runner.results)
assert len(runner.results) == 13
