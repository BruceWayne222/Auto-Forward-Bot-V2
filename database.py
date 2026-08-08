from os import environ 
from config import Config
import motor.motor_asyncio
from pymongo import MongoClient

async def mongodb_version():
    x = MongoClient(Config.DATABASE_URI)
    mongodb_version = x.server_info()['version']
    return mongodb_version

class Database:
    
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.bot = self.db.bots
        self.col = self.db.users
        self.nfy = self.db.notify
        self.chl = self.db.channels
        self.dup = self.db.duplicates
        self._dup_clients = {}
        
    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            ban_status=dict(
                is_banned=False,
                ban_reason="",
            ),
        )
      
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
    
    async def is_user_exist(self, id):
        user = await self.col.find_one({'id':int(id)})
        return bool(user)
    
    async def total_users_bots_count(self):
        bcount = await self.bot.count_documents({})
        count = await self.col.count_documents({})
        return count, bcount

    async def total_channels(self):
        count = await self.chl.count_documents({})
        return count
    
    async def remove_ban(self, id):
        ban_status = dict(
            is_banned=False,
            ban_reason=''
        )
        await self.col.update_one({'id': id}, {'$set': {'ban_status': ban_status}})
    
    async def ban_user(self, user_id, ban_reason="No Reason"):
        ban_status = dict(
            is_banned=True,
            ban_reason=ban_reason
        )
        await self.col.update_one({'id': user_id}, {'$set': {'ban_status': ban_status}})

    async def get_ban_status(self, id):
        default = dict(
            is_banned=False,
            ban_reason=''
        )
        user = await self.col.find_one({'id':int(id)})
        if not user:
            return default
        return user.get('ban_status', default)

    async def get_all_users(self):
        return self.col.find({})
    
    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
 
    async def get_banned(self):
        users = self.col.find({'ban_status.is_banned': True})
        b_users = [user['id'] async for user in users]
        return b_users

    async def update_configs(self, id, configs):
        await self.col.update_one({'id': int(id)}, {'$set': {'configs': configs}})
         
    async def get_configs(self, id):
        default = {
            'caption': None,
            'duplicate': True,
            'forward_tag': False,
            'file_size': 0,
            'size_limit': None,
            'extension': None,
            'keywords': None,
            'protect': None,
            'button': None,
            'db_uri': None,
            # Rate-limit / anti-ban settings
            'delay': 5,           # base seconds between each copy (balanced default)
            'safe_mode': True,    # extra jitter + adaptive slowdown + rest pauses
            'batch_size': 30,     # max messages per forward_messages batch
            'filters': {
               'poll': True,
               'text': True,
               'audio': True,
               'voice': True,
               'video': True,
               'photo': True,
               'document': True,
               'animation': True,
               'sticker': True
            }
        }
        user = await self.col.find_one({'id':int(id)})
        if user:
            configs = user.get('configs', default)
            # Ensure new keys exist for older users
            for key, val in default.items():
                if key not in configs:
                    configs[key] = val
            return configs
        return default 
       
    async def add_bot(self, datas):
       if not await self.is_bot_exist(datas['user_id']):
          await self.bot.insert_one(datas)
    
    async def remove_bot(self, user_id):
       await self.bot.delete_many({'user_id': int(user_id)})
      
    async def get_bot(self, user_id: int):
       bot = await self.bot.find_one({'user_id': user_id})
       return bot if bot else None
                                          
    async def is_bot_exist(self, user_id):
       bot = await self.bot.find_one({'user_id': user_id})
       return bool(bot)
                                          
    async def in_channel(self, user_id: int, chat_id: int) -> bool:
       channel = await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       return bool(channel)
    
    async def add_channel(self, user_id: int, chat_id: int, title, username):
       channel = await self.in_channel(user_id, chat_id)
       if channel:
         return False
       return await self.chl.insert_one({"user_id": user_id, "chat_id": chat_id, "title": title, "username": username})
    
    async def remove_channel(self, user_id: int, chat_id: int):
       channel = await self.in_channel(user_id, chat_id )
       if not channel:
         return False
       return await self.chl.delete_many({"user_id": int(user_id), "chat_id": int(chat_id)})
    
    async def get_channel_details(self, user_id: int, chat_id: int):
       return await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       
    async def get_user_channels(self, user_id: int):
       channels = self.chl.find({"user_id": int(user_id)})
       return [channel async for channel in channels]
     
    async def get_filters(self, user_id):
       filters = []
       filter = (await self.get_configs(user_id))['filters']
       for k, v in filter.items():
          if v == False:
            filters.append(str(k))
       return filters
              
    async def add_frwd(self, user_id):
       return await self.nfy.insert_one({'user_id': int(user_id)})
    
    async def rmve_frwd(self, user_id=0, all=False):
       data = {} if all else {'user_id': int(user_id)}
       return await self.nfy.delete_many(data)
    
    async def get_all_frwd(self):
       return self.nfy.find({})

    def _get_dup_collection(self, dup_uri=None):
       """Return the collection used to track forwarded file ids for duplicate-skip.
       If the user configured a custom database uri (Settings -> Database), use a
       'duplicates' collection there so history survives across bot restarts /
       redeploys. Otherwise fall back to the collection on the bot's own database
       (which still works, but only persists as long as this database does)."""
       if not dup_uri:
          return self.dup
       client = self._dup_clients.get(dup_uri)
       if client is None:
          client = motor.motor_asyncio.AsyncIOMotorClient(dup_uri)
          self._dup_clients[dup_uri] = client
       return client["AutoForwardBot"]["duplicates"]

    async def is_duplicate_file(self, file_unique_id, chat_id, dup_uri=None):
       """Return True if this file was already successfully forwarded to chat_id.
       Does NOT insert — call mark_file_forwarded() only after a successful send."""
       if not file_unique_id:
          return False
       try:
          collection = self._get_dup_collection(dup_uri)
          existing = await collection.find_one(
             {"chat_id": int(chat_id), "file_id": file_unique_id}
          )
          return existing is not None
       except Exception as e:
          print(f"[duplicate-check] failed, allowing message through: {e}")
          return False

    async def mark_file_forwarded(self, file_unique_id, chat_id, dup_uri=None):
       """Record a file as successfully forwarded so future runs skip it."""
       if not file_unique_id:
          return
       try:
          collection = self._get_dup_collection(dup_uri)
          await collection.update_one(
             {"chat_id": int(chat_id), "file_id": file_unique_id},
             {"$setOnInsert": {
                "chat_id": int(chat_id),
                "file_id": file_unique_id,
                "ts": __import__("time").time(),
             }},
             upsert=True,
          )
       except Exception as e:
          print(f"[duplicate-mark] failed: {e}")

    async def mark_files_bulk(self, file_unique_ids, chat_id, dup_uri=None):
       """Bulk-upsert many file ids (used when indexing the target chat)."""
       ids = list({fid for fid in file_unique_ids if fid})
       if not ids:
          return 0
       try:
          collection = self._get_dup_collection(dup_uri)
          import time as _t
          from pymongo import UpdateOne
          ts = _t.time()
          cid = int(chat_id)
          ops = [
             UpdateOne(
                {"chat_id": cid, "file_id": fid},
                {"$setOnInsert": {"chat_id": cid, "file_id": fid, "ts": ts}},
                upsert=True,
             )
             for fid in ids
          ]
          result = await collection.bulk_write(ops, ordered=False)
          return result.upserted_count or 0
       except Exception as e:
          print(f"[duplicate-bulk] failed: {e}")
          return 0

    async def clear_duplicate_history(self, chat_id, dup_uri=None):
       collection = self._get_dup_collection(dup_uri)
       await collection.delete_many({"chat_id": int(chat_id)})

db = Database(Config.DATABASE_URI, Config.DATABASE_NAME)
