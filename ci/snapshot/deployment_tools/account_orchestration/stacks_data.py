from deployment_tools.aws_managers.key_constants import TEMPLATE_NAME_KEY, PARAMETER_KEYS_KEY, PIPELINES_KEY

BUILD_TOOLS_ALARMS = {
    'alarms-build': {
        TEMPLATE_NAME_KEY: "alarms-build.yaml",
        PARAMETER_KEYS_KEY: ['BuildBatchPipeline',
                             'BuildCBMCLinuxPipeline',
                             'BuildDockerPipeline',
                             'BuildViewerPipeline',
                             'NotificationAddress',
                             'SIMAddress']
    }
}

BUILD_TOOLS_BUCKET_POLICY = {
    "bucket-policy": {
        TEMPLATE_NAME_KEY: "bucket-policy.yaml",
        PARAMETER_KEYS_KEY: ['ProofAccountIds',
                             'S3BucketToolsName']
    }
}

BUILD_TOOLS_CLOUDFORMATION_DATA = {
    "build-batch": {
        TEMPLATE_NAME_KEY: "build-batch.yaml",
        PARAMETER_KEYS_KEY: ['BatchRepositoryBranchName',
                             'BatchRepositoryName',
                             'BatchRepositoryOwner',
                             'GitHubToken',
                             'S3BucketName'],
        PIPELINES_KEY: ["Build-Batch-Pipeline"]
    },
    "build-cbmc-linux": {
        TEMPLATE_NAME_KEY: "build-cbmc-linux.yaml",
        PARAMETER_KEYS_KEY: ['CBMCBranchName',
                             'GitHubToken',
                             'S3BucketName'],
        PIPELINES_KEY: ["Build-CBMC-Linux-Pipeline"]
    },
    "build-docker": {
        TEMPLATE_NAME_KEY: "build-docker.yaml",
        PARAMETER_KEYS_KEY: ['BatchRepositoryBranchName',
                             'BatchRepositoryName',
                             'BatchRepositoryOwner',
                             'GitHubToken',
                             'S3BucketName'],
        PIPELINES_KEY: ["Build-Docker-Pipeline"]
    },
    "build-viewer": {
        TEMPLATE_NAME_KEY: "build-viewer.yaml",
        PARAMETER_KEYS_KEY: ['GitHubToken',
                             'S3BucketName',
                             'ViewerRepositoryBranchName',
                             'ViewerRepositoryName',
                             'ViewerRepositoryOwner'],
        PIPELINES_KEY: ["Build-Viewer-Pipeline"]
    }
}

BUILD_TOOLS_PACKAGES = {
    "template": {"extract": True}
}


CLOUDFRONT_CLOUDFORMATION_DATA = {
    "proof-results-cloudfront": {
        PARAMETER_KEYS_KEY: ['S3BucketProofs'],
        TEMPLATE_NAME_KEY: "cloudfront.yaml"
    }
}

GLOBALS_CLOUDFORMATION_DATA = {
    "globals": {
        TEMPLATE_NAME_KEY: "build-globals.yaml",
        PARAMETER_KEYS_KEY: ['BatchRepositoryBranchName',
                             'BatchRepositoryName',
                             'BatchRepositoryOwner',
                             'SnapshotID',
                             'ViewerRepositoryBranchName',
                             'ViewerRepositoryName',
                             'ViewerRepositoryOwner']
    }
}

PROOF_ACCOUNT_BATCH_CLOUDFORMATION_DATA = {
    "cbmc-batch": {
        TEMPLATE_NAME_KEY: "cbmc.yaml",
        PARAMETER_KEYS_KEY: ['BuildToolsAccountId',
                             'ImageTagSuffix',
                             "MaxVcpus"]
    },
    "alarms-prod": {
        TEMPLATE_NAME_KEY: "alarms-prod.yaml",
        PARAMETER_KEYS_KEY: ['NotificationAddress',
                             'ProjectName',
                             'SIMAddress']
    },
    "canary": {
        TEMPLATE_NAME_KEY: "canary.yaml",
        PARAMETER_KEYS_KEY: ['GitHubBranchName',
                             'GitHubLambdaAPI',
                             'GitHubRepository']
    }

}

PROOF_ACCOUNT_GITHUB_CLOUDFORMATION_DATA = {
    "github": {
        TEMPLATE_NAME_KEY: "github.yaml",
        PARAMETER_KEYS_KEY: ['BuildToolsAccountId',
                             'CloudfrontUrl',
                             'GitHubBranchName',
                             'GitHubRepository',
                             'ProjectName',
                             'S3BucketToolsName',
                             'GithubQueueUrl',
                             'SnapshotID']
    }
}

GITHUB_WORKER_DATA = {
    "github-worker": {
        TEMPLATE_NAME_KEY: "github_worker.yaml",
        PARAMETER_KEYS_KEY: ["SnapshotID", "S3BucketToolsName"]
    }
}

PROOF_ACCOUNT_PACKAGES = {
    "batch" : {"extract": False},
    "cbmc" : {"extract": False},
    "lambda": {"extract": False},
    "template": {"extract": True},
    "viewer": {"extract": False}
}
