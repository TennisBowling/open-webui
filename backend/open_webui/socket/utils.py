from open_webui.utils import fast_json as json
import uuid
from open_webui.utils.redis import get_redis_connection
from open_webui.env import REDIS_KEY_PREFIX
from typing import Optional, List, Tuple
import pycrdt as Y


class RedisLock:
    def __init__(
        self,
        redis_url,
        lock_name,
        timeout_secs,
        redis_sentinels=[],
        redis_cluster=False,
    ):

        self.lock_name = lock_name
        self.lock_id = str(uuid.uuid4())
        self.timeout_secs = timeout_secs
        self.lock_obtained = False
        self.redis = get_redis_connection(
            redis_url,
            redis_sentinels,
            redis_cluster=redis_cluster,
            decode_responses=True,
        )

    def aquire_lock(self):
        # nx=True will only set this key if it _hasn't_ already been set
        self.lock_obtained = self.redis.set(
            self.lock_name, self.lock_id, nx=True, ex=self.timeout_secs
        )
        return self.lock_obtained

    def renew_lock(self):
        # xx=True will only set this key if it _has_ already been set
        return self.redis.set(
            self.lock_name, self.lock_id, xx=True, ex=self.timeout_secs
        )

    def release_lock(self):
        lock_value = self.redis.get(self.lock_name)
        if lock_value and lock_value == self.lock_id:
            self.redis.delete(self.lock_name)


class RedisDict:
    def __init__(self, name, redis_url, redis_sentinels=[], redis_cluster=False):
        self.name = name
        self.redis = get_redis_connection(
            redis_url,
            redis_sentinels,
            redis_cluster=redis_cluster,
            decode_responses=True,
        )

    def __setitem__(self, key, value):
        serialized_value = json.dumps(value)
        self.redis.hset(self.name, key, serialized_value)

    def __getitem__(self, key):
        value = self.redis.hget(self.name, key)
        if value is None:
            raise KeyError(key)
        return json.loads(value)

    def __delitem__(self, key):
        result = self.redis.hdel(self.name, key)
        if result == 0:
            raise KeyError(key)

    def __contains__(self, key):
        return self.redis.hexists(self.name, key)

    def __len__(self):
        return self.redis.hlen(self.name)

    def keys(self):
        return self.redis.hkeys(self.name)

    def values(self):
        return [json.loads(v) for v in self.redis.hvals(self.name)]

    def items(self):
        return [(k, json.loads(v)) for k, v in self.redis.hgetall(self.name).items()]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def clear(self):
        self.redis.delete(self.name)

    def expire(self, ttl_seconds):
        """Set/refresh a TTL (in seconds) on the underlying Redis hash key.
        Returns True if the TTL was applied.

        Note: TTL is on the WHOLE hash, not per-field. Per-field deletions
        via __delitem__ do NOT clear or reset this TTL — Redis tracks expiry
        on the key itself."""
        try:
            return bool(self.redis.expire(self.name, int(ttl_seconds)))
        except Exception:
            return False

    def update(self, other=None, **kwargs):
        if other is not None:
            for k, v in other.items() if hasattr(other, "items") else other:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def setnx(self, key, value):
        """Atomically set hash field only if it does not exist. Returns True
        if the value was set, False if the field already had a value.

        Wraps Redis HSETNX so callers can perform a race-free claim against
        the underlying hash without a separate get/set round trip."""
        serialized_value = json.dumps(value)
        result = self.redis.hsetnx(self.name, key, serialized_value)
        return bool(result)

    def compare_and_swap(self, key, expected, new_value):
        """Atomically replace the value at `key` with `new_value` only if the
        current value equals `expected` (or `expected` is None and the field
        is absent). Returns True on success, False otherwise.

        Implemented as a Lua script so the read/compare/write is a single
        Redis operation, preventing races between concurrent callers."""
        expected_serialized = json.dumps(expected) if expected is not None else None
        new_serialized = json.dumps(new_value)
        script = """
        local current = redis.call('HGET', KEYS[1], ARGV[1])
        if current == false then
            if ARGV[2] == '__NIL__' then
                redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
                return 1
            end
            return 0
        end
        if current == ARGV[2] then
            redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
            return 1
        end
        return 0
        """
        result = self.redis.eval(
            script,
            1,
            self.name,
            key,
            "__NIL__" if expected_serialized is None else expected_serialized,
            new_serialized,
        )
        return bool(result)

    def compare_and_delete(self, key, expected):
        """Atomically delete `key` only if its current value equals
        `expected`. Returns True if the field was deleted."""
        expected_serialized = json.dumps(expected)
        script = """
        local current = redis.call('HGET', KEYS[1], ARGV[1])
        if current == ARGV[2] then
            redis.call('HDEL', KEYS[1], ARGV[1])
            return 1
        end
        return 0
        """
        result = self.redis.eval(
            script, 1, self.name, key, expected_serialized
        )
        return bool(result)


class YdocManager:
    def __init__(
        self,
        redis=None,
        redis_key_prefix: str = f"{REDIS_KEY_PREFIX}:ydoc:documents",
    ):
        self._updates = {}
        self._users = {}
        self._redis = redis
        self._redis_key_prefix = redis_key_prefix

    async def append_to_updates(self, document_id: str, update: bytes):
        document_id = document_id.replace(":", "_")
        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:updates"
            await self._redis.rpush(redis_key, json.dumps(list(update)))
        else:
            if document_id not in self._updates:
                self._updates[document_id] = []
            self._updates[document_id].append(update)

    async def get_updates(self, document_id: str) -> List[bytes]:
        document_id = document_id.replace(":", "_")

        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:updates"
            updates = await self._redis.lrange(redis_key, 0, -1)
            return [bytes(json.loads(update)) for update in updates]
        else:
            return self._updates.get(document_id, [])

    async def document_exists(self, document_id: str) -> bool:
        document_id = document_id.replace(":", "_")

        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:updates"
            return await self._redis.exists(redis_key) > 0
        else:
            return document_id in self._updates

    async def get_users(self, document_id: str) -> List[str]:
        document_id = document_id.replace(":", "_")

        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:users"
            users = await self._redis.smembers(redis_key)
            return list(users)
        else:
            return self._users.get(document_id, [])

    async def add_user(self, document_id: str, user_id: str):
        document_id = document_id.replace(":", "_")

        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:users"
            await self._redis.sadd(redis_key, user_id)
        else:
            if document_id not in self._users:
                self._users[document_id] = set()
            self._users[document_id].add(user_id)

    async def remove_user(self, document_id: str, user_id: str):
        document_id = document_id.replace(":", "_")

        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:users"
            await self._redis.srem(redis_key, user_id)
        else:
            if document_id in self._users and user_id in self._users[document_id]:
                self._users[document_id].remove(user_id)

    async def remove_user_from_all_documents(self, user_id: str):
        if self._redis:
            keys = await self._redis.keys(f"{self._redis_key_prefix}:*")
            for key in keys:
                if key.endswith(":users"):
                    await self._redis.srem(key, user_id)

                    document_id = key.split(":")[-2]
                    if len(await self.get_users(document_id)) == 0:
                        await self.clear_document(document_id)

        else:
            for document_id in list(self._users.keys()):
                if user_id in self._users[document_id]:
                    self._users[document_id].remove(user_id)
                    if not self._users[document_id]:
                        del self._users[document_id]

                        await self.clear_document(document_id)

    async def clear_document(self, document_id: str):
        document_id = document_id.replace(":", "_")

        if self._redis:
            redis_key = f"{self._redis_key_prefix}:{document_id}:updates"
            await self._redis.delete(redis_key)
            redis_users_key = f"{self._redis_key_prefix}:{document_id}:users"
            await self._redis.delete(redis_users_key)
        else:
            if document_id in self._updates:
                del self._updates[document_id]
            if document_id in self._users:
                del self._users[document_id]
