import asyncio
import logging
import os
import uuid

from aiohttp import web

from panini.managers.nats_client import NATSClient
from .exceptions import InitializingEventManagerError

from .http_server.http_server_app import HTTPServer
from .managers.event_manager import EventManager
from .managers.task_manager import TaskManager
from .middleware.error import ErrorMiddleware
from .utils import logger
from .utils.helper import (
    get_app_root_path,
    create_client_code_by_hostname,
)

_app = None


class App:
    def __init__(
            self,
            host: str,
            port: int,
            service_name: str = "panini_microservice_" + str(uuid.uuid4())[:10],
            client_id: str = None,
            reconnect: bool = False,
            max_reconnect_attempts: int = 60,
            reconnecting_time_sleep: int = 2,
            allocation_queue_group: str = "",

            logger_required: bool = True,
            logger_files_path: str = None,
            logger_in_separate_process: bool = False,
            pending_bytes_limit=65536 * 1024 * 10,
    ):
        """
        :param host: NATS broker host
        :param port: NATS broker port
        :param service_name: Name of microservice
        :param client_id: id of microservice, name and client_id used for NATS client name generating
        :param tasks: List of additional tasks
        :param reconnect: allows reconnect if connection to NATS has been lost
        :param max_reconnect_attempts: number of reconnect attempts
        :param reconnecting_time_sleep: pause between reconnection
        :param subscribe_subjects_and_callbacks: if you need to subscribe additional
                                        subjects(except subjects from event.py).
                                        This way doesn't support validators
        :param allocation_queue_group: name of NATS queue for distributing incoming messages among many NATS clients
                                    more detailed here: https://docs.nats.io/nats-concepts/queue
        :param listen_subject_only_if_include:   if not None, client will subscribe
                                                only to subjects that include these key words
        :param listen_subject_only_if_exclude:   if not None, client will not subscribe
                                                 to subjects that include these key words
        :param web_app: web.Application:       custom aiohttp app that you can create separately from panini.
                            if you set this argument client will only run this aiohttp app without handling
        :param web_host: Web application host
        :param web_port: Web application port
        :param logger_required: Is logger required for the project (if not - EmptyLogger will be provided)
        :param logger_files_path: main path for logs
        :param logger_in_separate_process: use log in the same or in different process
        """



        try:
            # client_id initialization
            if client_id is None:
                self.client_id = create_client_code_by_hostname(service_name)
            else:
                self.client_id = client_id
            os.environ["CLIENT_ID"] = self.client_id

            self.service_name = service_name

            self._event_manager = EventManager()
            self._task_manager = TaskManager()
            self._nats_client = NATSClient(
                host=host,
                port=port,
                client_id=self.client_id,
                allow_reconnect=reconnect,
                queue=allocation_queue_group,
                max_reconnect_attempts=max_reconnect_attempts,
                reconnecting_time_wait=reconnecting_time_sleep,
                pending_bytes_limit=pending_bytes_limit
            )

            self.app_root_path = get_app_root_path()
            self.logger_required = logger_required
            self.logger_in_separate_process = logger_in_separate_process

            # check, if TestClient is running
            if os.environ.get("PANINI_TEST_MODE"):
                self.logger_files_path = os.environ.get(
                    "PANINI_TEST_LOGGER_FILES_PATH", "test_logs"
                )
            else:
                self.logger_files_path = (
                    logger_files_path if logger_files_path else "logs"
                )
            self.logger = logger.Logger(None)

            if self.logger_required:
                self.set_logger(
                    self.service_name,
                    self.app_root_path,
                    self.logger_files_path,
                    False,
                    self.client_id,
                )

            self.logger_process = None
            self.log_stop_event = None
            self.log_listener_queue = None
            self.change_logger_config_listener_queue = None

            self.http_server = None
            self.http = None

            global _app
            _app = self

        except InitializingEventManagerError as e:
            error = f"App.event_registrar critical error: {str(e)}"
            raise InitializingEventManagerError(error)

    def setup_web_server(self, host=None, port=None, web_app=None):
        self.http = web.RouteTableDef()  # for http decorator
        if web_app:
            self.http_server = HTTPServer(base_app=self, web_app=web_app)
        else:
            self.http_server = HTTPServer(
                base_app=self, host=host, port=port
            )

    def filter_subjects(self, include: list = None, exclude: list = None):
        assert include and exclude, "You can use either include or exclude. Not both!"

        try:
            subscriptions = self._event_manager.subscriptions

            if include:
                for subject in subscriptions.copy():
                    success = False
                    for subject_include in include:
                        if subject_include in subject:
                            success = True
                            break
                    if not success:
                        del self._event_manager.subscriptions[subject]

            if exclude:
                for subject in subscriptions.copy():
                    for subject_exclude in exclude:
                        if subject_exclude in subject:
                            del self._event_manager.subscriptions[subject]
                            break


        except InitializingEventManagerError as e:
            error = f"App.event_registrar critical error: {str(e)}"
            raise InitializingEventManagerError(error)

    # TODO: implement change logging configuration during runtime to make it work properly
    def change_log_config(self, new_formatters: dict, new_handlers: dict):
        self.change_logger_config_listener_queue.put(
            {"new_formatters": new_formatters, "new_handlers": new_handlers}
        )
        raise NotImplementedError

    def set_logger(
            self,
            service_name,
            app_root_path,
            logger_files_path,
            in_separate_process,
            client_id,
    ):
        if in_separate_process:
            (
                self.log_listener_queue,
                self.log_stop_event,
                self.logger_process,
                self.change_logger_config_listener_queue,
            ) = logger.set_logger(
                service_name,
                app_root_path,
                logger_files_path,
                in_separate_process,
                client_id,
            )
        else:
            logger.set_logger(
                service_name,
                app_root_path,
                logger_files_path,
                in_separate_process,
                client_id,
            )
        self.logger.logger = logging.getLogger(service_name)

    def listen(
            self,
            subject: list or str,
            validator: type = None,
            dynamic_subscription=False,
            data_type="json.loads"
    ):
        return self._event_manager.listen(subject, validator, dynamic_subscription, data_type)

    def register_listener(self):
        pass

    async def publish(self, subject: str, message: dict):
        self._nats_client.client.publish(subject, message)

    async def request(self, subject, payload, timeout=0.5, expected=1, cb=None):
        return await self._nats_client.client.request(subject, payload, timeout, expected, cb)

    @property
    def task(self):
        return self._task_manager

    def register_single_task(self, task):
        self._task_manager.register_single_task(task=task)

    def register_interval_task(self, task, interval):
        self._task_manager.register_interval_task(task=task, interval=interval)

    def add_middleware(self, cls, *args, **kwargs):
        return self._nats_client.middleware_manager.add_middleware(cls, *args, **kwargs)

    def _start_event(self):
        asyncio.ensure_future(
            self._nats_client.client.publish(
                f"panini_events.{self.service_name}.{self.client_id}.started",
                b"{}",
            )
        )

    def start(self):
        if (
                os.environ.get("PANINI_TEST_MODE")
                and os.environ.get("PANINI_TEST_MODE_USE_ERROR_MIDDLEWARE", "false")
                == "true"
        ):
            def exception_handler(e, **kwargs):
                self.logger.exception(f"Error: {e}, for kwargs: {kwargs}")
                raise

            self.add_middleware(
                ErrorMiddleware, error=Exception, callback=exception_handler
            )
        if self.logger_required and self.logger_in_separate_process:
            self.set_logger(
                self.service_name,
                self.app_root_path,
                self.logger_files_path,
                True,
                self.client_id,
            )

        self._nats_client.set_listen_subjects_callbacks(self._event_manager.subscriptions)
        self._nats_client.start()

        if self.http_server:
            self.http_server.start_server()

        self._start_event()
        asyncio.get_event_loop().run_forever()



def get_app() -> App:
    return _app
