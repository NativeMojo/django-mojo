from typing import Optional, List, Set
from contextlib import contextmanager
from django.db import models
import redis.exceptions
from .client import get_connection
from ...errors import TimeoutException


class RedisBasePool:
    """Simple Redis pool using atomic Redis operations."""

    def __init__(self, pool_key: str, default_timeout: int = 30):
        """
        Initialize the Redis pool.

        Args:
            pool_key: Unique identifier for this pool
            default_timeout: Default timeout in seconds for blocking operations
        """
        self.pool_key = pool_key
        self.default_timeout = default_timeout
        self.redis_client = get_connection()

        self.available_list_key = f"{pool_key}:list"
        self.all_items_set_key = f"{pool_key}:set"

    def is_ready(self) -> bool:
        """Check if the pool is ready."""
        return self.redis_client.exists(self.available_list_key) and self.redis_client.exists(self.all_items_set_key)

    def add(self, str_id: str) -> bool:
        """Add an item to the pool."""
        if not isinstance(str_id, str):
            str_id = str(str_id)
        if not self.redis_client.sismember(self.all_items_set_key, str_id):
            self.redis_client.sadd(self.all_items_set_key, str_id)
            self.redis_client.lpush(self.available_list_key, str_id)
            return True
        return False

    def remove(self, str_id: str, force: bool = False) -> bool:
        """
        Remove an item from the pool entirely.

        Args:
            str_id: Item to remove
            force: If True, removes even if checked out. If False (default),
                   only removes if currently available.

        Returns:
            True if removed, False if not in pool or checked out (when force=False)
        """
        if not isinstance(str_id, str):
            str_id = str(str_id)
        if not self.redis_client.sismember(self.all_items_set_key, str_id):
            return False

        if not force:
            # Check if item is available (not checked out)
            available = self.redis_client.lrange(self.available_list_key, 0, -1)
            if str_id.encode() not in available and str_id not in available:
                return False

        self.redis_client.srem(self.all_items_set_key, str_id)
        self.redis_client.lrem(self.available_list_key, 0, str_id)
        return True

    def clear(self) -> None:
        """Clear all items from the pool."""
        self.redis_client.delete(self.available_list_key)
        self.redis_client.delete(self.all_items_set_key)

    def checkout(self, str_id: str, timeout: Optional[int] = None) -> bool:
        """Check out a specific item from the pool."""
        if not isinstance(str_id, str):
            str_id = str(str_id)
        if not self.redis_client.sismember(self.all_items_set_key, str_id):
            return False

        if timeout is None:
            removed = self.redis_client.lrem(self.available_list_key, 1, str_id)
            return removed > 0

        import time
        start = time.time()
        while self.redis_client.lrem(self.available_list_key, 1, str_id) == 0:
            time.sleep(1.0)
            elapsed = time.time() - start
            if elapsed > timeout:
                return False
        return True

    def checkin(self, str_id: str, allow_duplicate: bool = False) -> bool:
        """
        Check in an item back to the pool.

        Args:
            str_id: Item to check in
            allow_duplicate: If False (default), checks if item is already available
                           to prevent duplicates. If True, always adds to list.

        Returns:
            True if item was checked in, False if item not in pool or already available
        """
        if not isinstance(str_id, str):
            str_id = str(str_id)
        if not self.redis_client.sismember(self.all_items_set_key, str_id):
            return False

        # Prevent duplicates by checking if item is already in available list
        if not allow_duplicate:
            # Check if item is in the available list
            available = self.redis_client.lrange(self.available_list_key, 0, -1)
            if str_id.encode() in available or str_id in available:
                return False

        self.redis_client.lpush(self.available_list_key, str_id)
        return True

    def list_all(self) -> Set[str]:
        """List all items in the pool."""
        return self.redis_client.smembers(self.all_items_set_key)

    def list_available(self) -> List[str]:
        """List available items in the pool."""
        return self.redis_client.lrange(self.available_list_key, 0, -1)

    def list_checked_out(self) -> Set[str]:
        """List checked out items."""
        all_items = self.list_all()
        available_items = set(self.list_available())
        return all_items - available_items

    def destroy_pool(self) -> None:
        """Completely destroy the pool."""
        self.clear()

    def remove_duplicates(self) -> int:
        """
        Remove duplicate entries from the available list.

        Returns:
            Number of duplicates removed
        """
        available = self.redis_client.lrange(self.available_list_key, 0, -1)
        seen = set()
        duplicates_removed = 0

        # Clear the list
        self.redis_client.delete(self.available_list_key)

        # Re-add unique items only
        for item in reversed(available):  # Reversed to maintain original order
            if item not in seen:
                seen.add(item)
                self.redis_client.lpush(self.available_list_key, item)
            else:
                duplicates_removed += 1

        return duplicates_removed

    def get_next_available(self, timeout: Optional[int] = None) -> Optional[str]:
        """Get the next available item from the pool."""
        timeout = timeout or self.default_timeout

        try:
            result = self.redis_client.brpop(self.available_list_key, timeout=timeout)
            if result:
                return result[1]
            return None
        except redis.exceptions.TimeoutError:
            return None

    @contextmanager
    def checkout_item(self, timeout: Optional[int] = None, raise_on_timeout: bool = True):
        """Context manager for safely checking out and returning items."""
        item = self.get_next_available(timeout)
        if item is None:
            if raise_on_timeout:
                raise RuntimeError("No items available in pool")
            else:
                yield None
                return

        try:
            yield item
        finally:
            self.checkin(item)

    @contextmanager
    def checkout_specific_item(self, str_id: str, timeout: Optional[int] = None):
        """Context manager for checking out a specific item."""
        if not isinstance(str_id, str):
            str_id = str(str_id)
        if not self.checkout(str_id, timeout):
            raise RuntimeError(f"Could not checkout item {str_id}")

        try:
            yield str_id
        finally:
            self.checkin(str_id)


class RedisModelPool(RedisBasePool):
    """Django model-specific Redis pool."""

    def __init__(self, model_cls, query_dict, pool_key=None, default_timeout=30):
        """
        Initialize the model pool.

        Args:
            model_cls: Django model class
            query_dict: Query parameters to filter model instances
            pool_key: Unique identifier for this pool
            default_timeout: Default timeout in seconds
        """
        if pool_key is None:
            pool_key = f"modelpool:{model_cls.__name__}"
        super().__init__(pool_key, default_timeout)
        self.model_cls = model_cls
        self.query_dict = query_dict

    def init_pool(self) -> None:
        """Initialize pool with model instances."""
        self.destroy_pool()

        queryset = self.model_cls.objects.filter(**self.query_dict)
        for instance in queryset:
            item = str(instance.pk)
            # After destroy_pool(), the set is empty, so no need to check membership
            self.redis_client.sadd(self.all_items_set_key, item)
            self.redis_client.lpush(self.available_list_key, item)

    def add_to_pool(self, instance: models.Model) -> bool:
        """
        Add a model instance to the pool.

        Returns:
            True if item was added (either by init_pool or directly), False if it already existed
        """
        item = str(instance.pk)

        # Check if item exists before potential init
        existed_before = self.redis_client.exists(self.all_items_set_key) and \
                        self.redis_client.sismember(self.all_items_set_key, item)

        if not self.is_ready():
            self.init_pool()
            # If item didn't exist before but exists now, init_pool added it
            if not existed_before and self.redis_client.sismember(self.all_items_set_key, item):
                return True

        if not self.redis_client.sismember(self.all_items_set_key, item):
            self.redis_client.sadd(self.all_items_set_key, item)
            self.redis_client.lpush(self.available_list_key, item)
            return True
        return False

    def remove_from_pool(self, instance: models.Model, force: bool = False) -> bool:
        """
        Remove instance from pool.

        Args:
            instance: Model instance to remove
            force: If True, removes even if checked out. If False (default),
                   only removes if currently available.

        Returns:
            True if removed, False if not in pool or checked out (when force=False)
        """
        if not self.redis_client.exists(self.all_items_set_key):
            self.init_pool()

        item = str(instance.pk)
        if not self.redis_client.sismember(self.all_items_set_key, item):
            return False

        if not force:
            # Check if item is available (not checked out)
            available = self.redis_client.lrange(self.available_list_key, 0, -1)
            if item.encode() not in available and item not in available:
                return False

        self.redis_client.lrem(self.available_list_key, 0, item)
        self.redis_client.srem(self.all_items_set_key, item)
        return True

    def list_checked_out_instances(self):
        """List checked out items."""
        all_items = self.list_checked_out()
        return self.model_cls.objects.filter(pk__in=all_items)

    def get_next_instance(self, timeout: Optional[int] = None, _retries: int = 0, _max_retries: int = 100) -> Optional[models.Model]:
        """
        Get the next available model instance.

        Args:
            timeout: Timeout for waiting for an available instance
            _retries: Internal counter to prevent infinite recursion
            _max_retries: Maximum number of retries for stale records
        """
        if not self.redis_client.exists(self.all_items_set_key):
            self.init_pool()

        if _retries >= _max_retries:
            return None

        pk = self.get_next_available(timeout)
        if pk:
            try:
                instance = self.model_cls.objects.get(pk=pk)

                # Verify instance still matches criteria
                for key, value in self.query_dict.items():
                    if getattr(instance, key) != value:
                        self.redis_client.srem(self.all_items_set_key, pk)
                        return self.get_next_instance(timeout=0, _retries=_retries + 1, _max_retries=_max_retries)

                return instance
            except self.model_cls.DoesNotExist:
                self.redis_client.srem(self.all_items_set_key, pk)
                return self.get_next_instance(timeout=0, _retries=_retries + 1, _max_retries=_max_retries)

        return None

    def get_specific_instance(self, instance: models.Model) -> bool:
        """Get a specific instance from pool."""
        item = str(instance.pk)
        if not self.redis_client.sismember(self.all_items_set_key, item):
            return False

        removed = self.redis_client.lrem(self.available_list_key, 1, item)
        return removed > 0

    def return_instance(self, instance: models.Model, allow_duplicate: bool = False) -> bool:
        """
        Return a model instance to the pool.

        Args:
            instance: Model instance to return
            allow_duplicate: If False (default), prevents returning an already-available instance

        Returns:
            True if returned, False if not in pool or already available
        """
        item = str(instance.pk)
        if not self.redis_client.sismember(self.all_items_set_key, item):
            return False

        # Prevent duplicates by checking if item is already in available list
        if not allow_duplicate:
            available = self.redis_client.lrange(self.available_list_key, 0, -1)
            if item.encode() in available or item in available:
                return False

        self.redis_client.lpush(self.available_list_key, item)
        return True

    @contextmanager
    def checkout_instance(self, timeout: Optional[int] = None):
        """Context manager for safely checking out and returning model instances."""
        instance = self.get_next_instance(timeout)
        if instance is None:
            raise RuntimeError("No instances available in pool")

        try:
            yield instance
        finally:
            self.return_instance(instance)

    @contextmanager
    def checkout_specific_instance(self, instance: models.Model):
        """Context manager for checking out a specific model instance."""
        if not self.get_specific_instance(instance):
            raise RuntimeError(f"Could not checkout instance {instance.pk}")

        try:
            yield instance
        finally:
            self.return_instance(instance)


# Example usage:
if __name__ == "__main__":
    # Basic pool
    pool = RedisBasePool("test_pool")

    # Add items
    pool.add("item1")
    pool.add("item2")
    pool.add("item3")

    print("All items:", pool.list_all())
    print("Available:", pool.list_available())

    # Using context manager (recommended)
    try:
        with pool.checkout_item(timeout=5) as item:
            print(f"Got item: {item}")
            # Do work with item - automatically returned even if exception occurs
    except RuntimeError as e:
        print(f"No items available: {e}")

    # Django model example:
    # model_pool = RedisModelPool(
    #     model_cls=MyModel,
    #     query_dict={"status": "active"},
    #     pool_key="active_models"
    # )
    #
    # # Initialize pool
    # model_pool.init_pool()
    #
    # # Using context manager (recommended)
    # try:
    #     with model_pool.checkout_instance(timeout=30) as instance:
    #         print(f"Got instance: {instance}")
    #         # Do work with instance - automatically returned
    # except RuntimeError as e:
    #     print(f"No instances available: {e}")
