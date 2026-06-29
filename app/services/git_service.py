# app/services/git_service.py
"""
Git Service: Manages version control operations for database blueprints,
including commits, branching, pull requests, and DDL-compiling merges.
"""

import logging
from typing import Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import (
    ArchitectureCommit,
    ArchitectureBranch,
    ArchitecturePullRequest,
    ArchitectureComment
)
from app.engine.time_machine import generate_migration_plan

logger = logging.getLogger(__name__)


async def create_commit(
    db: AsyncSession,
    project_id: str,
    author_id: str,
    message: str,
    blueprint: dict,
    parent_id: Optional[str] = None
) -> ArchitectureCommit:
    """Creates a new architecture commit."""
    commit = ArchitectureCommit(
        project_id=project_id,
        parent_id=parent_id,
        author_id=author_id,
        message=message,
        blueprint=blueprint
    )
    db.add(commit)
    await db.flush()
    logger.info(f"💾 Created commit {commit.id[:8]} for project {project_id[:8]}")
    return commit


async def create_branch(
    db: AsyncSession,
    project_id: str,
    name: str,
    commit_id: str
) -> ArchitectureBranch:
    """Creates a new branch pointing to the specified commit."""
    branch = ArchitectureBranch(
        project_id=project_id,
        name=name,
        commit_id=commit_id
    )
    db.add(branch)
    await db.flush()
    logger.info(f"🌿 Created branch '{name}' pointing to commit {commit_id[:8]}")
    return branch


async def create_pull_request(
    db: AsyncSession,
    project_id: str,
    author_id: str,
    title: str,
    description: str,
    source_branch: str,
    target_branch: str
) -> ArchitecturePullRequest:
    """
    Creates an Architecture Pull Request.
    Triggers an automated review of the blueprint changes.
    """
    pr = ArchitecturePullRequest(
        project_id=project_id,
        title=title,
        description=description,
        source_branch=source_branch,
        target_branch=target_branch,
        status="open",
        author_id=author_id
    )
    db.add(pr)
    await db.flush()
    logger.info(f"🔀 Opened Pull Request #{pr.id[:8]}: {source_branch} -> {target_branch}")

    # Trigger automated AI review on the PR
    try:
        await trigger_pr_ai_review(db, pr)
    except Exception as e:
        logger.error(f"Failed to trigger automated PR AI review: {e}", exc_info=True)

    return pr


async def merge_pull_request(
    db: AsyncSession,
    pr_id: str,
    reviewer_id: str
) -> Tuple[str, ArchitectureCommit]:
    """
    Merges a Pull Request:
    1. Computes the DDL migration SQL between source and target branch commits.
    2. Creates a merge commit on the target branch containing the source blueprint.
    3. Updates the target branch pointer.
    4. Marks the PR as merged.
    """
    # 1. Load PR
    pr_query = await db.execute(select(ArchitecturePullRequest).where(ArchitecturePullRequest.id == pr_id))
    pr = pr_query.scalar_one()
    
    if pr.status != "open":
        raise ValueError("Pull Request is not open for merging.")

    # 2. Get source and target branch commits
    src_branch_q = await db.execute(
        select(ArchitectureBranch).where(
            ArchitectureBranch.project_id == pr.project_id,
            ArchitectureBranch.name == pr.source_branch
        )
    )
    src_branch = src_branch_q.scalar_one()

    tgt_branch_q = await db.execute(
        select(ArchitectureBranch).where(
            ArchitectureBranch.project_id == pr.project_id,
            ArchitectureBranch.name == pr.target_branch
        )
    )
    tgt_branch = tgt_branch_q.scalar_one()

    # Load blueprints
    src_commit_q = await db.execute(select(ArchitectureCommit).where(ArchitectureCommit.id == src_branch.commit_id))
    src_commit = src_commit_q.scalar_one()

    tgt_commit_q = await db.execute(select(ArchitectureCommit).where(ArchitectureCommit.id == tgt_branch.commit_id))
    tgt_commit = tgt_commit_q.scalar_one()

    # 3. Compile DDL migration SQL using Time Machine
    migration_plan = generate_migration_plan(
        old_blueprint=tgt_commit.blueprint,
        new_blueprint=src_commit.blueprint
    )
    migration_sql = migration_plan["migration_sql"]

    # 4. Create merge commit
    merge_commit = await create_commit(
        db=db,
        project_id=pr.project_id,
        author_id=reviewer_id,
        message=f"Merge pull request #{pr.id[:8]} from {pr.source_branch}",
        blueprint=src_commit.blueprint,
        parent_id=tgt_commit.id
    )

    # 5. Update target branch pointer
    tgt_branch.commit_id = merge_commit.id
    
    # 6. Close PR
    pr.status = "merged"
    pr.reviewer_id = reviewer_id

    logger.info(f"🏁 Merged Pull Request #{pr.id[:8]}. DDL Migration Compiled.")
    return migration_sql, merge_commit


# ── Internal AI PR Reviewer ──────────────────────────────────────────────────

async def trigger_pr_ai_review(db: AsyncSession, pr: ArchitecturePullRequest) -> None:
    """
    Performs an automated review of the PR blueprint diff and posts comments.
    """
    # Load source and target blueprints
    src_branch_q = await db.execute(
        select(ArchitectureBranch).where(
            ArchitectureBranch.project_id == pr.project_id,
            ArchitectureBranch.name == pr.source_branch
        )
    )
    src_branch = src_branch_q.scalar_one_or_none()

    tgt_branch_q = await db.execute(
        select(ArchitectureBranch).where(
            ArchitectureBranch.project_id == pr.project_id,
            ArchitectureBranch.name == pr.target_branch
        )
    )
    tgt_branch = tgt_branch_q.scalar_one_or_none()

    if not src_branch or not tgt_branch:
        return

    src_commit_q = await db.execute(select(ArchitectureCommit).where(ArchitectureCommit.id == src_branch.commit_id))
    src_commit = src_commit_q.scalar_one()

    tgt_commit_q = await db.execute(select(ArchitectureCommit).where(ArchitectureCommit.id == tgt_branch.commit_id))
    tgt_commit = tgt_commit_q.scalar_one()

    # Diff blueprints using Time Machine
    plan = generate_migration_plan(tgt_commit.blueprint, src_commit.blueprint)
    
    # Post automated comment summarizing the migration impact
    summary_comment = ArchitectureComment(
        pr_id=pr.id,
        author_id=pr.author_id,  # System/PR Author
        content=(
            f"🤖 **BaseAI Automated PR Review**\n\n"
            f"Comparing `{pr.source_branch}` -> `{pr.target_branch}`:\n"
            f"* **Added Tables**: {', '.join(plan['added_tables']) or 'None'}\n"
            f"* **Removed Tables**: {', '.join(plan['removed_tables']) or 'None'}\n\n"
            f"```sql\n"
            f"-- Compiled Migration DDL:\n"
            f"{plan['migration_sql']}\n"
            f"```"
        )
    )
    db.add(summary_comment)
