# Infra — LiveAvatar Advisor on AWS App Runner

This folder holds everything needed to host the backend on AWS.

```
infra/
├── bootstrap-oidc.yaml   # one-time per AWS account — GitHub OIDC + deploy role
├── apprunner.yaml        # the actual service: ECR + S3 + IAM + App Runner
└── README.md             # you are here
```

Pair them with:

```
.github/workflows/deploy-backend.yml   # builds, pushes to ECR, waits for App Runner
scripts/seed_chroma_s3.sh              # uploads chroma_db tarball for cold-start hydration
Dockerfile                             # the container that App Runner runs
```

The deploy model is:

```
git push main
   │
   ▼
GitHub Actions (deploy-backend.yml)
   │  assumes AWS role via OIDC (no long-lived keys)
   ▼
docker build → push to ECR (:sha-XXXX + :latest)
   │
   ▼  App Runner watches :latest with AutoDeployments
App Runner pulls the new image, calls /health, swaps traffic
   │
   ▼
Container boots → storage.py points at S3 users bucket
                → _ensure_chroma_db_present() pulls seed tarball from S3
                → secrets resolved from SSM Parameter Store
```

---

## One-time setup

### 1. Pick a region and an account

The templates default to whatever region you run `aws cloudformation deploy`
in. Most things in the project so far have run in `us-east-1`; stick with
it unless you have a reason not to.

```bash
export AWS_REGION=us-east-1
export AWS_PROFILE=your-profile-here
```

### 2. Put runtime secrets in SSM Parameter Store

The App Runner service does NOT bake secrets into the image or stack —
it reads them at container start. Create them as `SecureString`s under
`/liveavatar-advisor/`:

```bash
aws ssm put-parameter --name /liveavatar-advisor/OPENAI_API_KEY     --type SecureString --value "sk-..."
aws ssm put-parameter --name /liveavatar-advisor/LIVEAVATAR_API_KEY --type SecureString --value "..."
aws ssm put-parameter --name /liveavatar-advisor/ELEVENLABS_API_KEY --type SecureString --value "..."
aws ssm put-parameter --name /liveavatar-advisor/DEEPGRAM_API_KEY   --type SecureString --value "..."   # optional
```

To update one later, add `--overwrite`. App Runner re-resolves these at
each deploy, so you need to call `start-deployment` (or push a new image)
to pick up a rotated secret.

### 3. Deploy the OIDC bootstrap stack

This sets up the federation so GitHub can assume an AWS role without
us pasting long-lived keys into the repo.

```bash
aws cloudformation deploy \
  --stack-name liveavatar-advisor-oidc \
  --template-file infra/bootstrap-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      GitHubOrg=toNyc2017 \
      GitHubRepo=liveavatar-advisor \
      CreateOIDCProviderParam=true
```

> If this account already has the GitHub OIDC provider (from another
> project), pass `CreateOIDCProviderParam=false` instead.

Grab the role ARN:

```bash
aws cloudformation describe-stacks \
  --stack-name liveavatar-advisor-oidc \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text
```

### 4. Deploy the App Runner stack

```bash
aws cloudformation deploy \
  --stack-name liveavatar-advisor \
  --template-file infra/apprunner.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

This will sit at `CREATE_IN_PROGRESS` for the App Runner service until
it can pull an image from ECR — which is fine; we push the first image
in step 6.

Read the outputs:

```bash
aws cloudformation describe-stacks --stack-name liveavatar-advisor \
  --query 'Stacks[0].Outputs' --output table
```

You'll want `EcrRepoName`, `AppRunnerServiceArn`, `UsersBucketName`,
`ChromaSeedBucketName`, and `ServiceUrl`.

### 5. Configure GitHub repo variables

Settings → Secrets and variables → Actions → **Variables** (not Secrets;
none of these are sensitive):

| Variable                | Value                                           |
|-------------------------|-------------------------------------------------|
| `AWS_REGION`            | `us-east-1`                                     |
| `AWS_ACCOUNT_ID`        | your 12-digit account id                        |
| `AWS_DEPLOY_ROLE_ARN`   | output of `liveavatar-advisor-oidc` stack       |
| `ECR_REPO_NAME`         | output of `liveavatar-advisor` stack            |
| `APPRUNNER_SERVICE_ARN` | output of `liveavatar-advisor` stack            |

### 6. Seed the ChromaDB tarball

App Runner won't fully come up until this exists, because the backend
logs an error and refuses to serve RAG-grounded answers without it.

Locally, with chroma_db/ already built:

```bash
./scripts/seed_chroma_s3.sh
```

The script auto-detects the bucket name from the CloudFormation stack
outputs.

### 7. First deploy

Push to `main` (or run the `deploy-backend` workflow via `workflow_dispatch`).
First build takes ~5–8 minutes; subsequent ones are much faster thanks
to BuildKit layer caching in ECR.

Watch the run in the Actions tab. When the workflow's
**"Wait for App Runner to become healthy"** step turns green, the service
URL printed in the workflow summary is live.

---

## Day-2 operations

### Update the running image

Just push to `main`. The workflow handles the rest.

If you want to redeploy the **same** image (e.g. you just rotated a
secret in SSM and need the container to pick it up), run the workflow
manually with **Force App Runner deploy** = true.

### Rotate a secret

```bash
aws ssm put-parameter --name /liveavatar-advisor/OPENAI_API_KEY \
  --type SecureString --value "sk-new..." --overwrite
```

Then manually dispatch the workflow with **Force App Runner deploy**, or
push any trivial change to `main`.

### Re-seed ChromaDB after a corpus update

```bash
# 1. Re-ingest the corpus locally
source venv/bin/activate
python scripts/ingest.py

# 2. Upload the new tarball
./scripts/seed_chroma_s3.sh

# 3. Force App Runner to redeploy (so cold-start hydration runs again)
SERVICE_ARN=$(aws cloudformation describe-stacks \
  --stack-name liveavatar-advisor \
  --query "Stacks[0].Outputs[?OutputKey=='AppRunnerServiceArn'].OutputValue" --output text)
aws apprunner start-deployment --service-arn "$SERVICE_ARN"
```

> App Runner doesn't restart on its own when an S3 object changes — it
> only knows about ECR image updates. The `start-deployment` call is
> the trigger.

### Roll back to a previous image

```bash
# Find the sha tag you want
aws ecr describe-images --repository-name liveavatar-advisor \
  --query 'reverse(sort_by(imageDetails,& imagePushedAt))[].imageTags' --output table

# Re-tag it as :latest. App Runner will auto-deploy.
TAG="sha-abc123def456"
MANIFEST=$(aws ecr batch-get-image --repository-name liveavatar-advisor \
  --image-ids imageTag=$TAG --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name liveavatar-advisor \
  --image-tag latest --image-manifest "$MANIFEST"
```

### Inspect per-visitor memory in production

```bash
aws s3 ls s3://$(aws cloudformation describe-stacks --stack-name liveavatar-advisor \
  --query "Stacks[0].Outputs[?OutputKey=='UsersBucketName'].OutputValue" --output text)/users/
```

Same `users/<id>/USER.md` + `MEMORY.md` + `memory/<ts>.md` shape as
local dev — `storage.py` just swaps the read/write backend.

### Tail logs

App Runner streams stdout/stderr to CloudWatch Logs at
`/aws/apprunner/liveavatar-advisor/.../application`. The console's
"Logs" tab is the fastest way in.

---

## What this stack does NOT yet include

These are deliberate omissions — add them when there's a reason.

- **Custom domain.** App Runner gives you a `*.awsapprunner.com` URL
  out of the box. Pointing `advisor.olds.tom` (or wherever) at it is a
  separate `AWS::AppRunner::CustomDomain` resource + DNS records. Add
  when we move past ngrok-replacement.
- **Auth gating.** The service is currently public. NYM's Auth0 setup
  could be ported, or we can put CloudFront + Cognito in front. Defer
  until we know which client demos need it.
- **WAF / rate limiting.** Not configured. Add a `AWS::WAFv2::WebACL`
  attached to App Runner if abuse becomes a concern.
- **VPC connector.** Default egress to the public internet is fine
  (OpenAI, ElevenLabs, HeyGen are all public APIs). Switch to
  `EgressType: VPC` if you ever need to talk to a private RDS or
  similar.
- **Multi-region.** Single-region us-east-1. Cross-region failover is
  overkill for a demo and meaningful for production only.
