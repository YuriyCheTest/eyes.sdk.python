from __future__ import print_function

import queue
from collections import deque
from concurrent.futures.thread import ThreadPoolExecutor
from contextlib import contextmanager
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
                coroutine = coro_func(item)
                for res in coroutine:
                    if res is not None:
                        for output_queue in output_queues:
                            output_queue.append(res)
                    else:
                        try:
                            yield
                        except BaseException:
                            coroutine.close()
                            raise


def queue_for_each(input_queue, coro_proc, end_coro_proc):
    while True:
        if not input_queue:
            yield
        else:
            item = input_queue.popleft()
            if item is END:
                if end_coro_proc:
                    for _ in end_coro_proc:
                        try:
                            yield
                        except BaseException:
                            end_coro_proc.close()
                            raise
                break
            if coro_proc is not None:
                coroutine = coro_proc(item)
                for _ in coroutine:
                    try:
                        yield
                    except BaseException:
                        coroutine.close()
                        raise


class ParallelCoroutineGroup(object):
    def __init__(self, *coroutines, complete=True):
        self._coroutines = [iter(c) for c in coroutines]
        self._complete = complete
        self._added = None if complete else deque()

    def add(self, *coroutines):
        if self._complete:
            raise RuntimeError("Can't add to completed ParallelCoroutines")
        else:
            for coroutine in coroutines:
                self._added.append(iter(coroutine))

    def complete(self):
        if self._complete:
            raise RuntimeError("Already completed")
        else:
            self._added.append(END)

    @property
    def finished(self):
        return self._complete and not self._coroutines

    def coroutine(self):
        try:
            while not self.finished:
                while self._added:
                    added = self._added.popleft()
                    if added is END:
                        self._complete = True
                    else:
                        self._coroutines.append(added)
                finished = []
                for coroutine in self._coroutines:
                    try:
                        next(coroutine)
                    except StopIteration:
                        finished.append(coroutine)
                for coroutine in finished:
                    self._coroutines.remove(coroutine)
                yield
        except BaseException:
            for coroutine in self._coroutines:
                coroutine.close()
            raise


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
        self._session = None
        self._checks_queue = deque()
        self._collected_resources = deque()
        self.tests = [VGTest(b, results) for b in browsers_list]
        self._resource_collection_group = ParallelCoroutineGroup(complete=False)
        self._subtasks = ParallelCoroutineGroup(
            self._establish_connection(),
            queue_for_each(
                self._checks_queue,
                self._start_collect_resources_task,
                end_coro_proc=self._complete_collect_resources_group(),
            ),
            pipe_coroutine(
                self._collected_resources,
                None,
                *(t.collected_resources for t in self.tests)
            ),
            self._resource_collection_group.coroutine(),
            complete=False,
        )

    def add_check(self, check):
        log("Check {} defined", check)
        self._checks_queue.append(check)

    def complete(self):
        log("Test {} defined", self._name)
        self._checks_queue.append(END)

    def coroutine(self):
        coroutine = self._subtasks.coroutine()
        for _ in coroutine:
            if self._session:
                break
            else:
                yield

        self._subtasks.add(*(test.coroutine() for test in self.tests))
        self._subtasks.complete()
        for _ in coroutine:
            if self._resource_collection_group.finished:
                self._collected_resources.append(END)
                break
            yield
        for _ in coroutine:
            yield

        session_service.close_session(self._session)

    def _start_collect_resources_task(self, dom):
        self._resource_collection_group.add(self._collect_resources(dom))
        yield

    def _complete_collect_resources_group(self):
        self._resource_collection_group.complete()
        yield

    def _establish_connection(self):
        session_future = session_service.open_session(self._name)
        with auto_cancel(session_future):
            while not session_future.done():
                yield
            self._session = session_future.result()

    def _collect_resources(self, dom):
        collected_future = resource_collection_service.collect_resources(dom)
        with auto_cancel(collected_future):
            while not collected_future.done():
                yield
            self._collected_resources.append(collected_future.result())


class VGTest:
    def __init__(self, browser_info, results):
        self._browser_info = browser_info
        self.collected_resources = deque()
        render_results = deque()
        self._coroutines = ParallelCoroutineGroup(
            pipe_coroutine(self.collected_resources, self._render, render_results),
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
        self._coroutines = ParallelCoroutineGroup(complete=False)
        self._canceled = False

    def add_session(self, name, browsers):
        if self.sessions:
            self.sessions[-1].complete()
        self.sessions.append(VGTestSession(name, browsers, self.results))
        self._coroutines.add(self.sessions[-1].coroutine())
        return self.sessions[-1]

    def wait(self):
        if self._thread.is_alive():
            self.complete()
            self._thread.join()

    def complete(self):
        if self.sessions:
            self.sessions[-1].complete()
        self._coroutines.complete()

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
