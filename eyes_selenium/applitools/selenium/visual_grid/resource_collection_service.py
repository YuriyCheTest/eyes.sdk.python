import typing
from collections import Iterable, deque
from concurrent.futures.thread import ThreadPoolExecutor

from applitools.common import RGridDom, VGResource
from applitools.common.utils import apply_base_url
from applitools.selenium import logger
from applitools.selenium.parsers import collect_urls_from_

if typing.TYPE_CHECKING:
    from typing import Callable, Dict, List, NoReturn, Optional, Text, Tuple

    from applitools.selenium.visual_grid import EyesConnector, ResourceCache


class ResourceCollectionTask(object):
    def __init__(self, eyes_connector, resource_cache):
        self.eyes_connector = eyes_connector
        self.resource_cache = resource_cache
        self.full_request_resources = {}

    def parse_frame_dom_resources(self, data):  # noqa
        # type: (Dict) -> RGridDom
        base_url = data["url"]
        resource_urls = data.get("resourceUrls", [])
        all_blobs = data.get("blobs", [])
        frames = data.get("frames", [])
        logger.debug(
            """
        parse_frame_dom_resources() call

        base_url: {base_url}
        count blobs: {blobs_num}
        count resource urls: {resource_urls_num}
        count frames: {frames_num}

        """.format(
                base_url=base_url,
                blobs_num=len(all_blobs),
                resource_urls_num=len(resource_urls),
                frames_num=len(frames),
            )
        )

        def find_child_resource_urls(content_type, content, resource_url):
            # type: (Optional[Text], bytes, Text) -> NoReturn
            logger.debug(
                "find_child_resource_urls({0}, {1}) call".format(
                    content_type, resource_url
                )
            )
            if not content_type:
                logger.debug("content_type is empty. Skip handling of resources")
                return []
            return [
                apply_base_url(url, base_url, resource_url)
                for url in collect_urls_from_(content_type, content)
            ]

        frame_request_resources = {}
        for f_data in frames:
            f_data["url"] = apply_base_url(f_data["url"], base_url)
            frame_request_resources[f_data["url"]] = self.parse_frame_dom_resources(
                f_data
            ).resource

        urls_to_fetch = set(resource_urls)
        for blob in all_blobs:
            resource = VGResource.from_blob(blob, find_child_resource_urls)
            if resource.url.rstrip("#") == base_url:
                continue
            frame_request_resources[resource.url] = resource
            urls_to_fetch |= set(resource.child_resource_urls)

        resources_and_their_children = fetch_resources_recursively(
            urls_to_fetch,
            self.eyes_connector,
            self.resource_cache,
            find_child_resource_urls,
        )
        frame_request_resources.update(resources_and_their_children)
        self.full_request_resources.update(frame_request_resources)
        return RGridDom(
            url=base_url, dom_nodes=data["cdt"], resources=frame_request_resources
        )

    def collect_resources(self, dom):
        return self.parse_frame_dom_resources(dom), self.full_request_resources


class ResourceCollectionService(object):
    def __init__(self):
        self._executor = ThreadPoolExecutor(1, self.__class__.__name__)

    def collect_resources(self, dom, eyes_connector, resource_cache):
        def collect_resources_task():
            collector = ResourceCollectionTask(eyes_connector, resource_cache)
            return collector.collect_resources(dom)

        return self._executor.submit(collect_resources_task)


def fetch_resources_recursively(
    urls,  # type: Iterable[Text]
    eyes_connector,  # type: EyesConnector
    resource_cache,  # type: ResourceCache
    find_child_resource_urls,  # type: Callable[[Text, bytes, Text],List[Text]]
):
    # type: (...) -> Iterable[Tuple[Text, VGResource]]
    def get_resource(link):
        logger.debug("get_resource({0}) call".format(link))
        response = eyes_connector.download_resource(link)
        return VGResource.from_response(link, response, find_child_resource_urls)

    def schedule_fetch(urls):
        for url in urls:
            if url not in seen_urls:
                seen_urls.add(url)
                downloading = resource_cache.fetch_and_store(url, get_resource)
                if downloading:  # going to take time, add to the queue end
                    fetched_urls_deque.appendleft(url)
                else:  # resource is already in cache, add to the queue front
                    fetched_urls_deque.append(url)

    fetched_urls_deque = deque()
    seen_urls = set()
    schedule_fetch(urls)
    while fetched_urls_deque:
        url = fetched_urls_deque.pop()
        resource = resource_cache[url]
        if resource is None:
            logger.debug("No response for {}".format(url))
        else:
            schedule_fetch(resource.child_resource_urls)
            yield url, resource


instance = ResourceCollectionService()
