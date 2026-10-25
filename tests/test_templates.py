"""The app's default templates (which projects can override)"""
from pathlib import Path

from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.template import Context, Engine

import salesman_stripe_connect

# The app's own templates, not a project's overrides
ENGINE = Engine(dirs=[Path(salesman_stripe_connect.__file__).parent / "templates"])


def render_base(messages):
    template = ENGINE.get_template("salesman_stripe_connect/base.html")
    return template.render(Context({"messages": messages}))


def test_base_template_messages():
    html = render_base([Message(constants.SUCCESS, "Connected")])
    assert '<li class="success">Connected</li>' in html


def test_base_template_no_messages():
    assert '<ul class="messages">' not in render_base([])
