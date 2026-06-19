import logging
from urllib.parse import urlparse

import gevent
from ainterviewer.config import CONFIGS
from locust import (
    HttpUser,
    SequentialTaskSet,
    between,
    events,
    task,
)
from locust.exception import StopUser
from locust_plugins.users.socketio import SocketIOUser


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "--model-config",
        choices=list(CONFIGS.keys()),
        include_in_web_ui=True,
        default="default",
    )
    parser.add_argument(
        "--ws-path",
        choices=["ai", "test", "chat"],
        include_in_web_ui=True,
        default="ai",
    )
    # parser.add_argument("--my-argument", type=str, env_var="LOCUST_MY_ARGUMENT", default="", help="It's working")


class BrowsingUser(HttpUser):
    wait_time = between(2, 5)

    @task
    class BrowsingTasks(SequentialTaskSet):
        @task
        def index(self):
            self.client.get("/")

        @task
        def setup(self):
            self.client.get("/guide")

        @task
        def about(self):
            self.client.get("/about")


class InterviewUser(HttpUser, SocketIOUser):
    wait_time = between(1, 5)
    model_config = None
    token = None

    def on_start(self):
        print("User started")
        self.model_config = self.environment.parsed_options.model_config
        with self.client.get(
            f"/interview?model_config={self.model_config}"
        ) as response:
            self.token = response.cookies.get("token")

    @task
    def start_interview(self):
        host = urlparse(self.environment.parsed_options.host)
        ws_path = self.environment.parsed_options.ws_path
        websocket_endpoint = (
            f"{'wss' if host.scheme == 'https' else 'ws'}://{host.netloc}/ws/{ws_path}"
        )
        try:
            self.connect(
                websocket_endpoint,
                header={"Cookie": f"config={self.model_config};token={self.token}"},
            )
            gevent.sleep(20)
        except Exception as e:
            logging.warning(e)
        finally:
            raise StopUser()

    def on_stop(self):
        """Called when the User is stopped."""
        self.disconnect()

    def on_message(self, message):
        print("message received")

    def disconnect(self):
        """Here we actually close the connection."""
        try:
            if getattr(self, "ws_greenlet", None) is not None:
                # Inform the greenlet we're disconnecting.
                self.ws_greenlet.kill()
                self.ws_greenlet.join(timeout=1)

            if getattr(self, "ws", None) is not None:
                try:
                    self.ws.close()
                    print("WebSocket closed")
                except Exception as e:
                    print(f"Failed to close WebSocket connection: {e}")
        except Exception as e:
            logging.warning(e)
