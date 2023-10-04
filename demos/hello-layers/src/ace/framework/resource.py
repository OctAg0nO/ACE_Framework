from abc import ABC, abstractmethod

import logging
import json
import asyncio
import aio_pika
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from queue import Queue

from ace import constants
from ace.settings import Settings
from ace.api_endpoint import ApiEndpoint
from ace.amqp.connection import get_connection_and_channel

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Set the logger for pika, aiormq separately.
for base_logger in ['aio_pika', 'aiormq']:
    logging.getLogger(base_logger).setLevel(logging.INFO)


class Resource(ABC):
    def __init__(self):
        self.api_endpoint = ApiEndpoint(self.api_callbacks)
        self.bus_loop = asyncio.new_event_loop()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.connection = None
        self.channel = None
        self.consumers = {}
        self.consumer_local_queues = {}

    @property
    @abstractmethod
    def settings(self) -> Settings:
        pass

    @property
    def labeled_name(self):
        return f"{self.settings.name} ({self.settings.label})"

    @property
    def api_callbacks(self):
        return {
            'status': self.status
        }

    @abstractmethod
    def status(self):
        pass

    def return_status(self, up, data=None):
        data = data or {}
        data['up'] = up
        return data

    def connect_busses(self):
        logger.debug(f"{self.labeled_name} connecting to busses...")
        Thread(target=self.connect_busses_in_thread).start()

    def connect_busses_in_thread(self):
        asyncio.set_event_loop(self.bus_loop)
        self.bus_loop.run_until_complete(self.get_busses_connection_and_channel())
        self.bus_loop.run_until_complete(self.post_connect())
        self.bus_loop.run_forever()

    async def get_busses_connection_and_channel(self):
        logger.debug(f"{self.labeled_name} getting busses connection and channel...")
        self.connection, self.channel = await get_connection_and_channel(settings=self.settings, loop=self.bus_loop)
        logger.info(f"{self.labeled_name} busses connection established...")

    def disconnect_busses(self):
        logger.debug(f"{self.labeled_name} disconnecting from busses...")
        self.bus_loop.run_until_complete(self.pre_disconnect())
        self.bus_loop.run_until_complete(self.channel.close())
        self.bus_loop.run_until_complete(self.connection.close())
        self.bus_loop.call_soon_threadsafe(self.bus_loop.stop)
        logger.info(f"{self.labeled_name} busses connection closed...")

    async def post_connect(self):
        pass

    async def pre_disconnect(self):
        pass

    def start_resource(self):
        logger.info("Starting resource...")
        self.setup_service()
        logger.info("Resource started")

    def stop_resource(self):
        logger.info("Shutting down resource...")
        self.shutdown_service()
        logger.info("Resource shut down")

    def setup_service(self):
        logger.debug("Setting up service...")
        self.api_endpoint.start_endpoint()
        self.connect_busses()

    def shutdown_service(self):
        logger.debug("Shutting down service...")
        self.disconnect_busses()
        self.api_endpoint.stop_endpoint()

    def get_consumer_local_queue(self, queue_name):
        if queue_name not in self.consumer_local_queues:
            self.consumer_local_queues[queue_name] = Queue()
        return self.consumer_local_queues[queue_name]

    def push_message_to_consumer_local_queue(self, queue_name, message):
        self.get_consumer_local_queue(queue_name).put(message)

    def get_messages_from_consumer_local_queue(self, queue_name):
        messages = []
        queue = self.get_consumer_local_queue(queue_name)
        while not queue.empty():
            messages.append(queue.get())
        return messages

    def build_queue_name(self, direction, layer):
        queue = None
        if layer and direction in constants.LAYER_ORIENTATIONS:
            queue = f"{direction}.{layer}"
        return queue

    def build_exchange_name(self, direction, layer):
        exchange = None
        queue = self.build_queue_name(direction, layer)
        if queue:
            exchange = f"exchange.{queue}"
        return exchange

    def build_message(self, message=None, message_type='data'):
        message = message or {}
        message['type'] = message_type
        message['resource'] = self.settings.name
        return json.dumps(message).encode()

    async def publish_message(self, exchange_name, message, delivery_mode=2):
        exchange = await self.try_get_exchange(exchange_name)
        message = aio_pika.Message(
            body=message,
            delivery_mode=delivery_mode
        )
        await exchange.publish(message, routing_key="")

    def is_existant_layer_queue(self, orientation, idx):
        # Queue names are [direction].[destination_layer], so there is no:
        # 1. southbound to the first layer
        # 2. northbound to the last layer
        if (orientation == 'southbound' and idx == 0) or (orientation == 'northbound' and idx == len(self.settings.layers) - 1):
            return False
        return True

    def build_all_layer_queue_names(self):
        queue_names = []
        for orientation in constants.LAYER_ORIENTATIONS:
            for idx, layer in enumerate(self.settings.layers):
                if self.is_existant_layer_queue(orientation, idx):
                    queue_names.append(self.build_queue_name(orientation, layer))
        return queue_names

    async def try_queue_subscribe(self, queue_name, callback):
        while True:
            logger.debug(f"Trying to subscribe to queue: {queue_name}...")
            try:
                if self.channel.is_closed:
                    logger.info("Previous channel was closed, creating new channel...")
                    self.channel = await self.connection.channel()
                queue = await self.channel.get_queue(queue_name)
                await queue.consume(callback)
                logger.info(f"Subscribed to queue: {queue_name}")
                return
            except (aio_pika.exceptions.ChannelClosed, aio_pika.exceptions.ChannelClosed) as e:
                logger.warning(f"Error occurred: {str(e)}. Trying again in {constants.QUEUE_SUBSCRIBE_RETRY_SECONDS} seconds.")
                await asyncio.sleep(constants.QUEUE_SUBSCRIBE_RETRY_SECONDS)

    async def try_get_exchange(self, exchange_name):
        while True:
            logger.debug(f"Trying to get exchange: {exchange_name}...")
            try:
                if self.channel.is_closed:
                    logger.info("Previous channel was closed, creating new channel...")
                    self.channel = await self.connection.channel()
                exchange = await self.channel.get_exchange(exchange_name)
                return exchange
            except (aio_pika.exceptions.ChannelClosed, aio_pika.exceptions.ChannelClosed) as e:
                logger.warning(f"Error occurred: {str(e)}. Trying again in {constants.QUEUE_SUBSCRIBE_RETRY_SECONDS} seconds.")
                await asyncio.sleep(constants.QUEUE_SUBSCRIBE_RETRY_SECONDS)