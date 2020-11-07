from __future__ import print_function

import queue
from collections import deque
from concurrent.futures.thread import ThreadPoolExecutor
from threading import Condition, Thread, current_thread
from time import sleep, time

start = time()
END = object()


def log(template, *args):
    msg = template.format(*args)
    name = current_thread().name
    print("{:>20} {:5.2f} {}\n".format(name, time() - start, msg), end="")


def parallel_coroutines(coroutines):
    iterators = [iter(c) for c in coroutines]
    while iterators:
        finished = []
        for n, iterator in enumerate(iterators):
            try:
                next(iterator)
            except StopIteration:
                finished.append(n)
        for n in reversed(finished):
            del iterators[n]
        yield


def pop_deque_until(deque, sentinel):
    while True:
        if deque:
            item = deque.popleft()
            if item is sentinel:
                break
            else:
                yield item
        else:
            yield None


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
            return "session " + name

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


class RenderingService(ThreadPoolExecutor):
    def __init__(self):
        super(RenderingService, self).__init__(10, self.__class__.__name__)

    def render(self, dom):
        def do_render():
            log("Rendering: {}", dom)
            sleep(2)
            log("Done rendering: {}", dom)
            return "Rendered " + dom

        log("Scheduling render {}", dom)
        return self.submit(do_render)


class CheckService(ThreadPoolExecutor):
    def __init__(self):
        super(CheckService, self).__init__(10, self.__class__.__name__)

    def check(self, render_result):
        def do_check():
            log("Checking: {}", render_result)
            sleep(1)
            log("Done Checking: {}", render_result)
            return "Checked " + render_result

        log("Scheduling check {}", render_result)
        return self.submit(do_check)


class CollectionService(ThreadPoolExecutor):
    def __init__(self):
        super(CollectionService, self).__init__(10, self.__class__.__name__)

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
        self._browsers_list = browsers_list
        self._session = None
        self._checks_queue = deque()
        self._collected_resources = deque()
        self._execution_state = self._execution_generator()
        self._results = results

    def add_check(self, check):
        log("Check {} defined", check)
        self._checks_queue.append(check)

    def close(self):
        log("Test {} defined", self._name)
        self._checks_queue.append(END)

    def __iter__(self):
        return self._execution_state

    def _execution_generator(self):
        resource_collection = self._collect_resources_parallel()
        connection_establishment = self._establish_connection()
        for _ in parallel_coroutines([resource_collection, connection_establishment]):
            if not self._session:
                yield

        tests = [VGTest(b, self._results) for b in self._browsers_list]
        for _ in parallel_coroutines([resource_collection] + tests):
            if self._collected_resources:
                collected_dom = self._collected_resources.popleft()
                for t in tests:
                    t.add_check(collected_dom)
            else:
                yield

        session_service.close_session(self._session)

    def _establish_connection(self):
        session_future = session_service.open_session(self._name)
        while not session_future.done():
            yield
        self._session = session_future.result()

    def _collect_resources_parallel(self):
        collectors = []
        for dom in pop_deque_until(self._checks_queue, END):
            if dom:
                collectors.append(self._collect_resources(dom))
            for _ in parallel_coroutines(collectors):
                if self._checks_queue:
                    break
                else:
                    yield
            else:
                yield
        for _ in parallel_coroutines(collectors):
            yield
        self._collected_resources.append(END)

    def _collect_resources(self, dom):
        collected_future = resource_collection_service.collect_resources(dom)
        while not collected_future.done():
            yield
        self._collected_resources.append(collected_future.result())


class VGTest:
    def __init__(self, browser_info, results):
        self._browser_info = browser_info
        self._render_requests = deque()
        self._render_results_queue = deque()
        self._results = results

    def __iter__(self):
        for _ in parallel_coroutines([self._render(), self._check()]):
            yield

    def add_check(self, dom_with_resources):
        self._render_requests.append(dom_with_resources)

    def _render(self):
        for dom in pop_deque_until(self._render_requests, END):
            if dom:
                render_future = rendering_service.render(
                    "{} in {}".format(dom, self._browser_info)
                )
                while not render_future.done():
                    yield
                self._render_results_queue.append(render_future.result())
            else:
                yield
        self._render_results_queue.append(END)

    def _check(self):
        for render in pop_deque_until(self._render_results_queue, END):
            if render:
                check_future = check_service.check(render)
                while not check_future.done():
                    yield
                self._results.append(check_future.result())
            else:
                yield


class VGRunner(object):
    def __init__(self):
        self.results = []
        self._sessions_queue = deque()
        self._last_session = None
        self._canceled = False
        self._thread = Thread(target=self._run, name=self.__class__.__name__)

    def add_session(self, name, browsers):
        if self._last_session:
            self._last_session.close()
        self._last_session = VGTestSession(name, browsers, self.results)
        self._sessions_queue.append(self._last_session)
        return self._last_session

    def wait(self):
        if self._thread.is_alive():
            self.close()
            self._thread.join()

    def close(self):
        if self._last_session:
            self._last_session.close()
        self._sessions_queue.append(END)

    def cancel(self):
        self._canceled = True
        if self._thread.is_alive():
            self._thread.join()

    def _run(self):
        tests = []
        for test in pop_deque_until(self._sessions_queue, END):
            if self._canceled:
                break
            if test:
                tests.append(test)
            else:
                sleep(0.5)
            for _ in parallel_coroutines(tests):
                if self._canceled or self._sessions_queue:
                    break
                sleep(0.5)
        for _ in parallel_coroutines(tests):
            if self._canceled:
                break
            sleep(0.5)
        log("All is done, canceled: {}", self._canceled)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._thread.is_alive():
            self.cancel()


session_service = SessionService(2)
resource_collection_service = CollectionService()
rendering_service = RenderingService()
check_service = CheckService()
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
