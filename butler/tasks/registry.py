from collections.abc import Awaitable, Callable

TaskFn = Callable[[dict], Awaitable[dict]]

_registry: dict[str, TaskFn] = {}


def task(name: str):
    """Decorator that registers an async function as a runnable task by name."""

    def decorator(fn: TaskFn) -> TaskFn:
        _registry[name] = fn
        return fn

    return decorator


def get_task(name: str) -> TaskFn | None:
    return _registry.get(name)


def list_tasks() -> list[str]:
    return sorted(_registry)
