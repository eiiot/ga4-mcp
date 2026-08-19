import base64
import unittest

from analytics_mcp.gmail import normalize_message


class GmailToolsTest(unittest.TestCase):
    def test_normalize_message_decodes_headers_and_plain_text_body(self):
        body = (
            base64.urlsafe_b64encode(b"Hello from Gmail").decode().rstrip("=")
        )
        message = {
            "id": "message-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "snippet": "Hello",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Ada <ada@example.com>"},
                    {"name": "Subject", "value": "Status"},
                ],
                "mimeType": "text/plain",
                "body": {"data": body},
            },
        }

        self.assertEqual(
            normalize_message(message),
            {
                "id": "message-1",
                "thread_id": "thread-1",
                "label_ids": ["INBOX"],
                "snippet": "Hello",
                "from": "Ada <ada@example.com>",
                "to": None,
                "cc": None,
                "subject": "Status",
                "date": None,
                "body_text": "Hello from Gmail",
            },
        )


if __name__ == "__main__":
    unittest.main()
