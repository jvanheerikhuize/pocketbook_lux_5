"""Screenshot orientation across frontends without a reader or Linux framebuffer."""

import base64
import contextlib
import io
from unittest import mock

from pbtk import cli, paths
from pbtk.device import Device
from pbtk.gui.server import make_handler
from pbtk.image import Gray
from pbtk.mcp import Server
from support import IsolatedState


class ScreenshotViewTest(IsolatedState):
    def setUp(self):
        super().setUp()
        self.image = Gray(2, 3, bytes(range(6)))
        self.dev = Device(view="ccw")
        self.dev._agent = mock.Mock()
        self.dev._agent.fbshot.return_value = self.image

    def test_whole_defaults_to_view_but_explicit_rotation_overrides_it(self):
        for view in ("none", "cw", "ccw"):
            self.dev._view = view
            with self.subTest(view=view):
                image, _ = self.dev.screenshot(whole=True)
                self.assertEqual(image, self.image.turned(view))
                self.dev._agent.fbshot.assert_called_with("auto")
            for rotate in ("auto", "raw", "cw", "ccw"):
                with self.subTest(view=view, rotate=rotate):
                    image, _ = self.dev.screenshot(whole=True, rotate=rotate)
                    self.assertEqual(image, self.image)
                    self.dev._agent.fbshot.assert_called_with(rotate)

    def test_cli_omitted_and_explicit_rotation(self):
        for extra, expected in (([], self.image.turned("ccw")), (["--rotate", "auto"], self.image)):
            with self.subTest(extra=extra), mock.patch("pbtk.cli._dev", return_value=self.dev):
                path = self.tmp / "cli.png"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["shot", "--whole", "-o", str(path), *extra]), 0)
                self.assertEqual(path.read_bytes(), expected.png())

    def test_mcp_omitted_and_explicit_rotation(self):
        server = Server()
        server.dev = self.dev
        for args, expected in (({}, self.image.turned("ccw")), ({"rotate": "auto"}, self.image)):
            with self.subTest(args=args):
                content = server.call("pb_screenshot", {"whole": True, **args})
                image = next(item for item in content if item["type"] == "image")
                self.assertEqual(base64.b64decode(image["data"]), expected.png())

    def test_gui_live_preview_uses_view(self):
        app = mock.Mock(dev=self.dev)
        app.agent.return_value = self.dev._agent
        handler_type = make_handler(app)
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/screen.png?whole=1"
        handler._send = mock.Mock()
        handler.do_GET()
        self.assertEqual(handler._send.call_args.args[1], self.image.turned("ccw").png())

    def test_gui_saved_screenshot_uses_view(self):
        app = mock.Mock(dev=self.dev)
        handler_type = make_handler(app)
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/screenshot"
        handler._same_origin = mock.Mock(return_value=True)
        handler._body = mock.Mock(return_value={"whole": True})
        handler._json = mock.Mock()
        handler.do_POST()
        result = handler._json.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertEqual((paths.shots_dir() / result["shot"]).read_bytes(),
                         self.image.turned("ccw").png())
