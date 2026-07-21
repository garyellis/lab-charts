from azure.cosmos import ContainerProxy
from chart_manager.services.events.lifecycle import PlatformLifecycleEvent

class CosmosEventStore:
    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    def write(self, event: PlatformLifecycleEvent) -> None:
        if event.correlation_id is None:
            raise ValueError("correlation_id is required (it is the partition key)")
        item = event.to_dict()
        item["id"] = item["uuid"]  # cosmos requires a string 'id'
        self._container.create_item(item)
