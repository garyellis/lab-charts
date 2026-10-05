"""`event emit build|promote` and `event list`: write and read platform lifecycle events.

The package exports the `event list` wire document. The events themselves live in
`shared/events`.
"""

from chart_manager.commands.events.wire import events_to_dict

__all__ = ["events_to_dict"]
