# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import tarfile
import urllib

import boto3
import github

from cbmc_ci_timer import Timer
CBMC_RETRY_KEYWORDS = ["CBMC_RETRY", "/cbmc run checks"]

def update_github_status(repo_id, sha, status, ctx, desc, jobname, post_url = False):
    target_url = None

    if jobname and post_url:
        cloudfront_url = os.environ['CLOUDFRONT_URL']
        target_url = (f"https://{cloudfront_url}/{jobname}/out/html/index.html")

    updating = os.environ.get('CBMC_CI_UPDATING_STATUS')
    queue_url = os.environ.get("GITHUB_QUEUE_URL")
    pr = sha.replace("origin/pr/", "") if "origin/pr/" in sha else None
    if updating and updating.strip().lower() == 'true':
        update_github_msg = {
            "repo_id": repo_id,
            "oath": get_github_personal_access_token(),
            "commit": sha,
            "pr": pr,
            "status": status,
            "context": "CBMC Batch: " + ctx,
            "description": desc,
            "cloudfront_url": target_url
        }
        sqs = boto3.client("sqs")
        print(f"Sending a message to the Github worker queue: {json.dumps(update_github_msg, indent=2)}")
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(update_github_msg), MessageGroupId=sha)
        return

    print("Not updating GitHub status")

def get_github_personal_access_token():
    """
    Get plaintext for GitHub Personal Access Token (needed for updating commit
    statuses)
    """
    sm = boto3.client('secretsmanager')
    s = sm.get_secret_value(SecretId='GitHubCommitStatusPAT')
    return str(json.loads(s['SecretString'])[0]['GitHubPAT'])


def update_status(status, ctx, jobname, desc, repo_id, sha, no_status_metric, post_url = False):
    """Update GitHub Status

    Relevant documentation:
    https://developer.github.com/v3/repos/statuses/#create-a-status
    http://pygithub.readthedocs.io/en/latest/github_objects/Commit.html
    """

    #pylint: disable=too-many-arguments

    region = os.environ['AWS_REGION']

    status_to_metric = {
        "pending": "Attempts",
        "error": "Errors",
        "success": "Successes",
        "failure": "Failures"
    }
    cloudwatch = boto3.client("cloudwatch", region_name=region)

    if not no_status_metric:
        cloudwatch.put_metric_data(
            MetricData=[
                {
                    'MetricName': status_to_metric[status],
                    'Unit': 'None',
                    'Value': 1.0
                },
            ],
            Namespace=os.environ['PROJECT_NAME']
        )

    timer = Timer("Updating GitHub status {} with description {}".format(
        status, desc))
    try:
        update_github_status(repo_id, sha, status, ctx, desc, jobname, post_url=post_url)
        cloudwatch.put_metric_data(
            MetricData=[
                {
                    'MetricName': 'GitHub status update succeeded',
                    'Unit': 'None',
                    'Value': 1.0
                },
            ],
            Namespace=os.environ['PROJECT_NAME']
        )
    #pylint: disable=broad-except
    except Exception as e:
        print("Failed to update status on GitHub: " + str(e))
        cloudwatch.put_metric_data(
            MetricData=[
                {
                    'MetricName': 'GitHub status update failed',
                    'Unit': 'None',
                    'Value': 1.0
                },
            ],
            Namespace=os.environ['PROJECT_NAME']
        )
    timer.end()


def fetch_tar(tar_path, tar_URL):
    """
    Download tar_URL to tar_path.

    GitHub may require authorization in case of private repositories. This is
    accomplished by setting an "Authorization" HTTP header to the GitHub
    personal access (OAuth) token.
    """
    print("downloading {} to {}".format(tar_URL, tar_path))
    token = get_github_personal_access_token()
    request = urllib.request.Request(
        url=tar_URL, headers={"Authorization": "token " + token})
    response = urllib.request.urlopen(request)
    with open(tar_path, "wb") as tar:
        tar.write(response.read())


def extract_tar(tmp_dir, tar_path):
    """
    Extract a tar archive downloaded from GitHub.

    All files in the archive should be contained in a single top-level
    directory - the code below actually checks that all files share a common
    prefix, which must be a directory. The name of that directory varies, and
    is thus found as the common prefix and returned to the caller.
    """
    print("Extracting tar file {} to directory {}".format(tar_path, tmp_dir))

    prefix = None
    with tarfile.open(tar_path) as tar:
        paths = tar.getnames()
        for p in paths:
            if p.startswith('/') or '..' in p:
                raise ValueError("Invalid filename")
        prefix = os.path.commonprefix(paths)

        kwds = {"path": tmp_dir}
        # The FreeRTOS repository is larger than the writeable disk
        # space available in a lambda.  Our solution is to omit a 250M
        # directory not needed by current CBMC proofs.  This is a
        # project-specific solution that will be replaced in the
        # future by a general solution that extracts only the
        # cbmc-batch.yaml files needed to launch the CBMC proofs.
        if "freertos" in tar_path:
            kwds["members"] = [member
                               for member in tar.getmembers()
                               if "lib/third_party/mcu_vendor"
                               not in member.name]

        timer = Timer("Extract tar containing code for commit")
        tar.extractall(**kwds)
        timer.end()

    print(str(os.listdir(tmp_dir)))
    if not prefix or not os.path.isdir(os.path.join(tmp_dir, prefix)):
        raise ValueError("No common root base directory found")

    return prefix

def get_tar(name, full_name, sha, tmp_dir):
    """Get tar containing the code for the commit"""
    timer = Timer("Get tar containing code for commit")
    tar_name = sha + ".tar.gz"
    tar_URL = "https://api.github.com/repos/{}/tarball/{}".format(
        full_name, str(sha))
    tar_file = full_name.replace('/', '-') + "-" + tar_name
    tar_path = os.path.join(tmp_dir, tar_file)
    fetch_tar(tar_path, tar_URL)
    timer.end()

    timer = Timer("Uploading tar containing code for commit to S3")
    s3 = boto3.client('s3')
    s3.upload_file(
        Bucket=os.environ['S3_BUCKET_PROOFS'], Key=tar_file, Filename=tar_path)
    timer.end()

    return (tar_path, tar_file)

def parse_pr(body):
    """
    Parse the pull request event body for the base and head branches,
    and get the repository name, repository id, and branch name for the base
    branch, and get the sha for the head commit on the head branch.

    event has a format as described here:
        https://developer.github.com/v3/activity/events/types/#pullrequestevent

    """
    action = body["action"]
    if action not in ["opened", "synchronize"]:
        print("Ignoring pull request with action {}".format(action))
        return (None, None, None, None, None)

    head_repo_name = body["pull_request"]["head"]["repo"]["full_name"]
    head_sha = body["pull_request"]["head"]["sha"]

    base_repo_name = body["pull_request"]["base"]["repo"]["full_name"]
    base_repo_id = body["pull_request"]["base"]["repo"]["id"]
    base_repo_branch = body["pull_request"]["base"]["ref"]
    draft = body["pull_request"]["draft"]

    print("Pull request: {action} {from_repo} -> {to_repo} (draft: {d})".format(
        action=action, from_repo=head_repo_name, to_repo=base_repo_name,
        d=draft))

    # This optimization interacts badly with FreeRTOS branch filtering.
    #
    # For historical reasons, FreeRTOS CI restricts attention to a small
    # collection of branches like master, and ignores all github events
    # except for a push to or a pull request against one of these
    # branches.  For special case of a pull request pushed directly to the
    # repository (and not a fork), the branch filtering skips checking the
    # push and this optimization skips checking the pull request, meaning
    # nothing gets checked.
    #
    # if head_sha is not None and head_repo_name == base_repo_name:
    #     print("Ignoring pull request action as base repository matches head")
    #     return (None, None, None, None, None)

    return (base_repo_name, base_repo_id, base_repo_branch, head_sha, draft)

def parse_issue_type(body):
    """
    Parse the pull request event body for the base and head branches,
    and get the repository name, repository id, and branch name for the base
    branch, and get the sha for the head commit on the head branch.

    event has a format as described here:
        https://developer.github.com/v3/activity/events/types/#pullrequestevent

    """
    comment_text = body["comment"]["body"]
    if comment_text not in CBMC_RETRY_KEYWORDS:
        # We ignore all events that are not retry keywords
        return (None, None, None, None, None)

    base_repo_name = body["repository"]["full_name"]
    base_repo_id = body["repository"]["id"]
    base_repo_branch = "COMMENT_RETRY"
    draft = False  # FIXME: We are assuming always not a draft
    pr_num = body["issue"]["number"]
    head_sha = f"origin/pr/{pr_num}"
    return (base_repo_name, base_repo_id, base_repo_branch, head_sha, draft)







def parse_push(body):
    """
    Parse the push event body and get the repository name, repository id,
    and branch name for the branch being pushed to, and get the sha
    for the head commit on the branch being pushed.

    event has a format as described here:
        https://developer.github.com/v3/activity/events/types/#pushevent
    """

    repo_name = body["repository"]["full_name"]
    repo_id = body["repository"]["id"]
    repo_branch = body["ref"]

    head_commit = body.get("head_commit")
    if head_commit:
        print("Push to {}: {}".format(repo_name, body["ref"]))
        sha = head_commit["id"]
    else:
        print("Ignoring delete-branch push event")
        sha = None
    return (repo_name, repo_id, repo_branch, sha, False)

def parse_event(event):
    """
    Get the source code, repo ID, and SHA for the GitHub commit in the event.

    event is a json data structure containing a header and a payload.
    The header format is described here: https://developer.github.com/webhooks/
    The payload, which contains a string that can be parsed into json for the
    type of GitHub event has a format as described here:
        https://developer.github.com/v3/activity/events/types/
    We expect to handle PushEvents and PullRequestEvents
    """
    print("GitHub event:")
    print(json.dumps(event))
    body = json.loads(event["body"])
    print("GitHub event body:")
    print(json.dumps(body))

    headers = {k.lower(): v for k, v in event["headers"].items()}
    event_type = headers["x-github-event"]
    if event_type == "pull_request":
        return parse_pr(body)
    if event_type == "push":
        return parse_push(body)
    if event_type == "issue_comment":
        return parse_issue_type(body)
    else:
        print(f"Unhandled webhook event type: '{event_type}'. Ignoring...")
        return (None, None, None, None, None)

    # raise ValueError("Unexpected event type: {}".format(event_type))
