# Continuous deployment over Tailscale

Every push to `main` runs [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. **check:** byte-compiles the Python and runs `shellcheck`. This also runs on pull requests.
2. **deploy:** runs only for pushes to `main` and manual runs. The GitHub runner joins
   your tailnet as an *ephemeral* `tag:ci` node using Tailscale's official action,
   authenticating with GitHub's OIDC token, so no Tailscale secret is stored in GitHub. It
   then connects over SSH to the device with a key that can run exactly one command:
   `deploy <commit sha>`.

On the device, [`deploy.sh`](deploy.sh):

- Takes a lock, so only one deploy runs at a time.
- Verifies the requested commit is on `origin/main`, and skips commits older than the
  one currently running.
- **Drains:** stops the worker from starting new jobs, then waits for the current job
  to finish. Uploads are still accepted and stay in the queue during this time.
- Checks out the new commit with `git reset --hard`. If `requirements.txt` changed, it
  updates Python packages, keeping the Jetson builds of torch and torchvision. If the
  systemd unit file changed, it reinstalls it.
- Restarts the service and waits until `/api/status` reports the new commit.
- **Rolls back** to the previous commit and restarts if any step fails, then marks the
  workflow as failed.

The workflow log shows only commit hashes and progress messages. Host names, IPs and
server logs stay on the device in `data/deploy.log` and `data/server.log`.

## One-time setup

Replace `<user>`, `<device>` and `<device-tailscale-name>` with your own values.

### 1. Device: make the app directory a git clone

If you installed from a clone, skip this step. Otherwise convert the existing
directory in place; `data/`, `.venv/` and `infer.env` are gitignored and kept:

```sh
cd ~/orin-inference
git init -q -b main
git remote add origin https://github.com/<owner>/orin-inference.git
git fetch -q origin main
git reset --hard origin/main
systemctl --user restart orin-inference
```

### 2. Device: create a deploy key that can only run the deploy

```sh
ssh-keygen -t ed25519 -N '' -C orin-inference-ci -f ~/orin-ci-deploy-key
echo "restrict,command=\"$HOME/orin-inference/deploy/ssh-entry.sh\" $(cat ~/orin-ci-deploy-key.pub)" \
  >> ~/.ssh/authorized_keys
```

`restrict` disables port forwarding, agent forwarding, X11 forwarding and terminal
allocation. `command=` forces every login with this key to run `ssh-entry.sh`, which
only accepts `deploy <40-char sha>`.

Save the private key (`~/orin-ci-deploy-key`) for step 4, then delete both key files
from the device.

### 3. Tailscale admin console

**Create the tag** and **limit CI to SSH on the device.** In *Access controls*, add the
following. If your policy still has the default allow-all rule, `tag:ci` can reach
everything. Remove that rule or narrow it so this one decides what CI can reach:

```jsonc
"tagOwners": {
  "tag:ci": ["autogroup:admin"],
},
"grants": [
  // CI can reach only the device's SSH port, nothing else on the tailnet.
  { "src": ["tag:ci"], "dst": ["<device>"], "ip": ["tcp:22"] },
],
```

`<device>` can be the device's Tailscale IP, a host alias defined in `hosts`, or a tag
you apply to it, such as `tag:orin`.

**Create a federated identity credential** (workload identity federation) in
*Settings → Trust credentials*:

- **Issuer:** GitHub Actions.
- **Subject:** `repo:<owner>/orin-inference:environment:production`. Only jobs that run
  in this repo's `production` environment get a matching token, so other branches,
  pull requests and forks can't join the tailnet.
- **Scope:** **Auth Keys: write**, with the tag `tag:ci`.

Copy the **client ID** and **audience** it shows. CI nodes are ephemeral, so Tailscale
removes them as soon as the job ends.

### 4. GitHub: environment and secrets

1. *Settings → Environments → New environment* named **`production`**. Under
   *Deployment branches and tags*, choose *Selected branches* and add `main`.
   Secrets in this environment are never available to pull requests or other branches.
2. Add these **environment secrets** to `production`. The client ID and audience aren't
   sensitive on their own, but secrets keep everything in one place and are masked in
   the public workflow log:

| Secret | Value |
| --- | --- |
| `TS_CLIENT_ID` | Client ID of the federated identity from step 3 |
| `TS_AUDIENCE` | Audience of the federated identity from step 3 |
| `DEPLOY_HOST` | Device's Tailscale IP or MagicDNS name |
| `DEPLOY_USER` | Device user that runs the service |
| `DEPLOY_SSH_KEY` | Contents of the private key from step 2 |
| `DEPLOY_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519 <device-tailscale-name>`, run from a machine on the tailnet |

3. Optional but recommended: protect `main` (*Settings → Branches*) so changes arrive
   through reviewed pull requests. Anyone who can push to `main` can deploy code to the device.

### 5. Test it

Run the workflow manually: *Actions → Deploy → Run workflow* on `main`. It should
report `already at <sha>` or `deployed <sha>`.

## Operations

```sh
tail -f ~/orin-inference/data/deploy.log                     # deploy history
~/orin-inference/deploy/deploy.sh "$(git -C ~/orin-inference rev-parse origin/main)"  # deploy by hand
```

To roll back, revert the bad commit on `main`. The revert is a new commit, so it deploys
normally. `deploy.sh` never moves the device to an older commit by itself.

If a deploy dies partway through, the worker may still be paused. The pause expires on
its own after 3 hours, or you can resume immediately with
`rm ~/orin-inference/data/PAUSE`. The page shows "paused for deploy" while the worker
is paused.
