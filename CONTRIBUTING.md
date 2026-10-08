# Contributing

Rules for working in the `hxyl` repositories. Server: http://8.145.35.173:3000/

## Already enforced by the server

- Nobody pushes directly to `main`. Every change reaches `main` through a pull request (PR).
- Nobody can force-push to or delete `main`.
- All repos live under the `hxyl` organization and are private.

## The workflow

1. **Branch** from the latest `main`, one branch per task:
   ```bash
   git checkout main && git pull
   git checkout -b wip/<yourname>/<topic>      # e.g. wip/liyang/edge-detection
   ```
2. **Commit and push to your branch at least once a day**, even if the work is unfinished
   or does not build yet. No PR or approval is needed to push to your own branch.
   ```bash
   git push -u origin wip/<yourname>/<topic>   # first push; afterwards just: git push
   ```
3. **Open a PR into `main`** when the work is ready (or earlier, as a draft with `WIP:` in the title).
4. **The repo owner reviews and merges** it (squash merge, the default).
5. **The branch is deleted automatically** on merge. Clean up locally and start the next task:
   ```bash
   git checkout main
   git pull
   git branch -D wip/<yourname>/<topic>
   git checkout -b wip/<yourname>/<next-topic>
   ```

Branches are not private: everyone in the team can see them. That is intentional, so progress
is visible and work can be taken over. The name says who owns the branch.

If a branch lives longer than about a week, merge `main` into it regularly so the final merge
doesn't hit a pile of conflicts:
```bash
git pull origin main
git push
```

## How to open a pull request

### Option 1: Website (recommended)

1. Push your branch (`git push -u origin wip/<yourname>/<topic>`).
2. Open the repo in the browser, e.g. http://8.145.35.173:3000/hxyl/welding. Forgejo shows a
   **"You pushed to … New Pull Request"** banner. Click it.
   Without the banner: **Pull Requests** tab, then **New Pull Request**.
3. Set **merge into: `main`** ← **pull from: `wip/<yourname>/<topic>`**.
4. Review the commits and changes Forgejo shows, then click **New Pull Request**.
5. Fill in the form:
   - **Title**: what it does, e.g. "Add plate thickness check". Start it with `WIP:` if not ready yet.
   - **Description**: complete the template ("What & why", "How tested"). Add `Closes #12` to
     close the issue on merge.
   - **Right sidebar**: Reviewers = repo owner, Assignee = yourself.
6. Click **Create Pull Request**.

### Option 2: Straight from git push

```bash
git push origin HEAD:refs/for/main -o topic=<topic> -o title="Add plate thickness check"
```

### Option 3: Command-line tool

Install `fj` (Forgejo CLI) or `tea` (Gitea CLI, works with Forgejo):
```bash
tea pr create --base main --title "Add plate thickness check"
```

### After opening

- **Need changes?** Keep committing and pushing to the same branch. The PR updates itself.
- **Review comments:** fix the code, push, then reply or mark the comment as resolved.
- **Merging (owner):** click **Merge**, using "Create squash commit".
- **Conflicts with `main`:**
  ```bash
  git pull origin main      # resolve conflicts, commit
  git push
  ```

## Commit messages

Say what changed and why:
```
Add plate thickness check before cutting

Thin plates (<3mm) warped during plasma cut; reject them early.
```
Avoid messages like "update" or "fix" on their own. Small commits are fine.

## Never commit

- Passwords, API keys, tokens, private keys. Use repo **Settings → Actions → Secrets**,
  or a local `.env` file listed in `.gitignore`.
- Large files (over about 50 MB): data sets, models, videos. Use Git LFS or object storage; ask first.
- Build output, `node_modules/`, `__pycache__/`, virtual environments. Add them to `.gitignore`.

## Keep CLAUDE.md current

`CLAUDE.md` is the handover document for people and AI agents picking up this repo.
- Update its **Current status** section at least weekly, with a date.
- Any PR that changes setup, architecture or behavior updates `CLAUDE.md` in the same PR.

## Issues

Each task is an issue, assigned to one person, in the repo where the work happens.
The progress dashboard reads issues and PRs, so work that isn't tracked won't appear there.

## Repo owners

Each repo's owners are listed in its `.forgejo/CODEOWNERS` file, and they are
automatically asked to review every PR. The team dashboard (http://8.145.35.173:3001/)
shows the owners of every repo. The owner reviews PRs, keeps `CLAUDE.md` accurate and
decides on structure. Anyone can still contribute through PRs.

## New repositories

Create new repos under the `hxyl` organization, not under your personal account.
Within about 10 minutes of the first push, the server applies the standard setup:
it adds `CONTRIBUTING.md`, `CLAUDE.md` and the PR template if they are missing,
makes you and biru the owners, and protects `main`. From then on, work on
`wip/<yourname>/<topic>` branches and merge through PRs.

## When someone leaves

1. Push all branches and update `CLAUDE.md` with what is in progress and what is next.
2. Reassign their open issues and PRs.
3. The admin disables the account. Code and history stay in the repos.
