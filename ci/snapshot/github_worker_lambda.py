import os
import json
import time
from math import floor, ceil
from datetime import datetime
from time import sleep
from update_github import GithubUpdater

import boto3
TIME_LIMIT_MINUTES = 5

queue_name = os.getenv("GITHUB_QUEUE_NAME")

class Sqs:
    def __init__(self, queue_name=None):
        if queue_name is None:
            raise Exception("Missing Github Queue name")
        self.sqs = boto3.client("sqs")
        self.sqs_resource = boto3.resource("sqs")
        print(f"Trying to get resource for queue with name: {queue_name}")
        self.queue = self.sqs_resource.get_queue_by_name(QueueName=queue_name)

    def delete_message(self, m):
        self.queue.delete_messages(
            Entries=[
                {
                    'Id': m.message_id,
                    "ReceiptHandle": m.receipt_handle
                },
            ]
        )
    def receive_message(self):
        return self.queue.receive_messages(MaxNumberOfMessages=10)

def lambda_handler(event, request):
    sqs = Sqs(queue_name=queue_name)
    g = None

    # Run for 10 minutes
    t_end = time.time() + 60 * TIME_LIMIT_MINUTES
    while time.time() < t_end:
        for m in sqs.receive_message():
            github_msg = json.loads(m.body)
            print(json.dumps(github_msg, indent=2))

            # We should only create the GithubUpdater once
            # since it uses up some of our API limit
            if g is None:
                g = GithubUpdater(repo_id=int(github_msg["repo_id"]),
                                  oath_token=github_msg["oath"])
            print(f"Github object: {g}")
            if g.remaining_calls == 0:
                raise Exception(f"Hit the Github API ratelimit. Failed to deliver message:{json.dumps(github_msg, indent=2)}")
            elif g.remaining_calls <= g.seconds_to_reset:
                # Exit, we cannot process this call right now
                print("We are running out API calls, going to sleep without pushing to GitHub")
                return
            cloudfront_url = github_msg["cloudfront_url"] if "cloudfront_url" in github_msg else None
            commit_sha = github_msg["commit"] if "commit" in github_msg else None
            pull_request = github_msg["pr"] if "pr" in github_msg else None
            g.update_status(status=github_msg["status"], proof_name=github_msg["context"], commit_sha=commit_sha,
                            pull_request=pull_request, cloudfront_url=cloudfront_url, description=github_msg["description"])
            sqs.delete_message(m)


