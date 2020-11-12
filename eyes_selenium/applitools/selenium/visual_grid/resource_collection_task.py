import typing
from itertools import chain

import attr

from applitools.common import (
    EyesError,
    RenderInfo,
    RenderRequest,
    RGridDom,
    VisualGridSelector,
    logger,
)
from applitools.common.utils.converters import str2bool
from applitools.common.utils.general_utils import get_env_with_prefix

from . import resource_collection_and_upload_service
from .vg_task import VGTask

if typing.TYPE_CHECKING:
    from typing import Any, Callable, Dict, List, Optional, Text

    from applitools.common import Region, RenderingInfo
    from applitools.core import ServerConnector
    from applitools.selenium.visual_grid import ResourceCache, RunningTest


@attr.s(hash=True)
class ResourceCollectionTask(VGTask):
    MAX_FAILS_COUNT = 5
    MAX_ITERATIONS = 2400  # poll_render_status for 1 hour

    script = attr.ib(hash=False, repr=False)  # type: Dict[str, Any]
    resource_cache = attr.ib(hash=False, repr=False)  # type: ResourceCache
    put_cache = attr.ib(hash=False, repr=False)
    server_connector = attr.ib(hash=False, repr=False)  # type: ServerConnector
    rendering_info = attr.ib()  # type: RenderingInfo
    region_selectors = attr.ib(
        hash=False, factory=list
    )  # type: List[List[VisualGridSelector]]
    size_mode = attr.ib(default=None)
    region_to_check = attr.ib(hash=False, default=None)  # type: Region
    script_hooks = attr.ib(hash=False, default=None)  # type: Optional[Dict]
    agent_id = attr.ib(default=None)  # type: Optional[Text]
    selector = attr.ib(hash=False, default=None)  # type: Optional[VisualGridSelector]
    func_to_run = attr.ib(default=None, hash=False, repr=False)  # type: Callable
    running_tests = attr.ib(hash=False, factory=list)  # type: List[RunningTest]
    request_options = attr.ib(hash=False, factory=dict)  # type: Dict[str, Any]
    is_force_put_needed = attr.ib(
        default=str2bool(get_env_with_prefix("APPLITOOLS_UFG_FORCE_PUT_RESOURCES"))
    )  # type: bool

    def __attrs_post_init__(self):
        # type: () -> None
        self.func_to_run = lambda: self.prepare_data_for_rg(
            self.script
        )  # type: Callable

    def prepare_data_for_rg(self, data):
        # type: (Dict) -> List[RenderRequest]
        resource_service = resource_collection_and_upload_service.instance
        future = resource_service.collect_and_upload_resources(
            data,
            self.server_connector,
            self.resource_cache,
            self.put_cache,
            self.is_force_put_needed,
        )
        dom, full_request_resources = future.result()
        render_requests = self.prepare_rg_requests(dom, full_request_resources)
        logger.debug(
            "exit - returning render_request array of length: {}".format(
                len(render_requests)
            )
        )
        return render_requests

    def prepare_rg_requests(self, dom, request_resources):
        # type: (RGridDom, Dict) -> Dict[RunningTest,RenderRequest]
        if self.size_mode == "region" and self.region_to_check is None:
            raise EyesError("Region to check should be present")
        if self.size_mode == "selector" and not isinstance(
            self.selector, VisualGridSelector
        ):
            raise EyesError("Selector should be present")
        requests = {}
        region = None
        for running_test in self.running_tests:
            if self.region_to_check:
                region = dict(
                    x=self.region_to_check.x,
                    y=self.region_to_check.y,
                    width=self.region_to_check.width,
                    height=self.region_to_check.height,
                )
            r_info = RenderInfo.from_(
                size_mode=self.size_mode,
                selector=self.selector,
                region=region,
                render_browser_info=running_test.browser_info,
            )

            requests[running_test] = RenderRequest(
                webhook=self.rendering_info.results_url,
                agent_id=self.agent_id,
                url=dom.url,
                stitching_service=self.rendering_info.stitching_service_url,
                dom=dom,
                resources=request_resources,
                render_info=r_info,
                renderer=running_test.eyes.renderer,
                browser_name=running_test.browser_info.browser,
                platform_name=running_test.browser_info.platform,
                script_hooks=self.script_hooks,
                selectors_to_find_regions_for=list(chain(*self.region_selectors)),
                send_dom=running_test.configuration.send_dom,
                options=self.request_options,
            )
        return requests
