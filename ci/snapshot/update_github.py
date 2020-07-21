import json
import time
from datetime import datetime
from math import floor

import github

class GithubUpdater:
    GIT_SUCCESS = "success"
    GIT_FAILURE = "failure"

    def __init__(self, repo_id=None, oath_token=None, session_uuid=None):
        self.session_uuid = session_uuid
        self.g = github.Github(oath_token)
        self.repo = self.g.get_repo(repo_id)
        self.seconds_to_reset = None
        self.remaining_calls = None
        self.time_to_reset = None
        self.get_rate_limit()
        self.get_reset_time()
        print(f"remaining_calls: {self.remaining_calls}")
        print(f"time_to_reset {self.time_to_reset}")
        print(f"total seconds: {self.seconds_to_reset}")

    def update_status(self, status=GIT_SUCCESS, proof_name=None, commit_sha=None, cloudfront_url=None,
                      description=None, pull_request=None):

        kwds = {'state': status,
                'context': proof_name,
                'description': description}
        if cloudfront_url is not None:
            kwds["target_url"] = cloudfront_url
        print(f"Updating github status with the following parameters:\n{json.dumps(kwds, indent=2)}")
        if commit_sha is None and pull_request is None:
            raise Exception(f"Invalid github update request for proof {proof_name}."
                            f" Must provide either commit sha or pull request info")
        start = time.time()
        if pull_request is not None:
            commit_sha = self.repo.get_pull(int(pull_request)).head.sha
        self.repo.get_commit(sha=commit_sha).create_status(**kwds)
        end = time.time()
        print(f"Status update took {end - start} seconds")
        self.remaining_calls -= 1
        print(f"Remaining API calls: {self.remaining_calls}")
        return

    def get_rate_limit(self):
        rl = self.g.get_rate_limit()
        core = rl.core
        self.remaining_calls = core.remaining
        print(f"Rate limit remaining: {self.remaining_calls}")
        return core.remaining
    def get_reset_time(self):
        rtime = self.g.rate_limiting_resettime
        dt_object = datetime.fromtimestamp(rtime)
        self.time_to_reset = dt_object - datetime.now()
        self.seconds_to_reset = floor(self.time_to_reset.total_seconds())
        print(f"Seconds to reset: {self.seconds_to_reset}")
        return self.seconds_to_reset
