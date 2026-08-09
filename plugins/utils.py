import time as tm
from database import db 
from .test import parse_buttons

STATUS = {}

class STS:
    def __init__(self, id):
        self.id = id
        self.data = STATUS
    
    def verify(self):
        return self.data.get(self.id)
    
    def store(self, From, to,  skip, limit, continuous=False):
        self.data[self.id] = {"FROM": From, 'TO': to, 'total_files': 0, 'skip': skip, 'limit': limit,
                      'fetched': skip, 'filtered': 0, 'deleted': 0, 'duplicate': 0, 'total': limit, 'start': 0, 'continuous': continuous}
        self.get(full=True)
        return STS(self.id)
        
    def get(self, value=None, full=False):
        values = self.data.get(self.id)
        if not full:
           return values.get(value)
        for k, v in values.items():
            setattr(self, k, v)
        return self

    def add(self, key=None, value=1, time=False):
        if time:
          return self.data[self.id].update({'start': tm.time()})
        self.data[self.id].update({key: self.get(key) + value}) 
    
    def divide(self, no, by):
       by = 1 if int(by) == 0 else by 
       return int(no) / by 
    
    async def get_data(self, user_id):
        # Fetch bot + config together. The old implementation called
        # get_configs() twice (once indirectly through get_filters()), which
        # could make /fwd appear stuck on "Loading settings..." when MongoDB
        # was slow. Keep a single config read and derive filters locally.
        import asyncio

        bot_task = asyncio.create_task(db.get_bot(user_id))
        config_task = asyncio.create_task(db.get_configs(user_id))
        bot, configs = await asyncio.gather(bot_task, config_task)

        filter_config = configs.get('filters') or {}
        filters = [str(k) for k, v in filter_config.items() if v is False]

        if configs.get('duplicate'):
            duplicate = [configs.get('db_uri'), self.TO]
        else:
            duplicate = False

        button = parse_buttons(configs.get('button') or '')
        size = None
        if configs.get('file_size', 0) != 0:
            size = [configs.get('file_size'), configs.get('size_limit')]

        return bot, configs.get('caption'), configs.get('forward_tag', False), {
                'chat_id': self.FROM,
                'limit': self.limit,
                'offset': self.skip,
                'filters': filters,
                'keywords': configs.get('keywords'),
                'media_size': size,
                'extensions': configs.get('extension'),
                'skip_duplicate': duplicate,
                'delay': configs.get('delay', 5),
                'safe_mode': configs.get('safe_mode', True),
                'batch_size': configs.get('batch_size', 30),
            }, configs.get('protect'), button

