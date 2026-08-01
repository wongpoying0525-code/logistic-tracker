from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


OVERVIEW_COLUMNS = [
    "单号",
    "物流公司",
    "重量(kg)",
    "包裹件数",
    "发往仓库",
    "最新物流状态",
    "更新时间",
]


def create_db_engine(database_url: str) -> Engine:
    """创建可复用的 PostgreSQL SQLAlchemy Engine。"""
    database_url = str(database_url or "").strip()

    if not database_url:
        raise ValueError("DATABASE_URL 不能为空")

    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=300,
        future=True,
    )


def load_packages(engine: Engine) -> pd.DataFrame:
    """读取全部物流总览记录。"""
    query = text(
        """
        select
            tracking_number as "单号",
            carrier as "物流公司",
            weight_kg as "重量(kg)",
            package_count as "包裹件数",
            coalesce(destination, '') as "发往仓库",
            latest_status as "最新物流状态",
            coalesce(
                to_char(
                    last_checked_at
                    at time zone 'Asia/Singapore',
                    'YYYY-MM-DD HH24:MI:SS'
                ),
                ''
            ) as "更新时间"
        from public.packages
        order by created_at desc, tracking_number
        """
    )

    with engine.connect() as connection:
        dataframe = pd.read_sql_query(
            query,
            connection,
        )

    if dataframe.empty:
        return pd.DataFrame(
            columns=OVERVIEW_COLUMNS
        )

    dataframe["单号"] = (
        dataframe["单号"].astype("string")
    )

    dataframe["重量(kg)"] = pd.to_numeric(
        dataframe["重量(kg)"],
        errors="coerce",
    )

    dataframe["包裹件数"] = pd.to_numeric(
        dataframe["包裹件数"],
        errors="coerce",
    ).astype("Int64")

    return dataframe[
        OVERVIEW_COLUMNS
    ].reset_index(drop=True)


def load_package_targets(
    engine: Engine,
) -> list[dict[str, Any]]:
    """读取需要调用承运商 API 的全部单号。"""
    query = text(
        """
        select
            tracking_number,
            carrier
        from public.packages
        order by created_at, tracking_number
        """
    )

    with engine.connect() as connection:
        rows = connection.execute(
            query
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


def package_exists(
    engine: Engine,
    tracking_number: str,
) -> bool:
    """检查物流单号是否已经存在。"""
    query = text(
        """
        select exists (
            select 1
            from public.packages
            where tracking_number = :tracking_number
        )
        """
    )

    with engine.connect() as connection:
        exists = connection.execute(
            query,
            {
                "tracking_number":
                    tracking_number,
            },
        ).scalar()

    return bool(exists)


def load_tracking_events(
    engine: Engine,
    tracking_number: str,
) -> list[dict[str, Any]]:
    """读取一个物流单号的完整轨迹。"""
    query = text(
        """
        select
            coalesce(event_date, '') as "日期",
            coalesce(event_time, '') as "时间",
            coalesce(location, '') as "处理地点",
            coalesce(status, '') as "物流状态"
        from public.tracking_events
        where tracking_number = :tracking_number
        order by event_order
        """
    )

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "tracking_number":
                    tracking_number,
            },
        ).mappings().all()

    return [
        dict(row)
        for row in rows
    ]


def load_all_tracking_events(
    engine: Engine,
) -> dict[str, list[dict[str, Any]]]:
    """读取全部物流轨迹，用于 Excel 导出。"""
    query = text(
        """
        select
            tracking_number,
            event_date,
            event_time,
            location,
            status
        from public.tracking_events
        order by
            tracking_number,
            event_order
        """
    )

    with engine.connect() as connection:
        rows = connection.execute(
            query
        ).mappings().all()

    details: dict[
        str,
        list[dict[str, Any]]
    ] = {}

    for row in rows:
        tracking_number = str(
            row["tracking_number"]
        )

        details.setdefault(
            tracking_number,
            [],
        ).append(
            {
                "日期":
                    row["event_date"] or "",
                "时间":
                    row["event_time"] or "",
                "处理地点":
                    row["location"] or "",
                "物流状态":
                    row["status"] or "",
            }
        )

    return details


def save_tracking_result(
    engine: Engine,
    tracking_number: str,
    carrier: str,
    weight_kg: float | None,
    package_count: int | None,
    destination: str,
    latest_status: str,
    timeline: list[dict[str, Any]],
) -> None:
    """
    在同一个数据库事务中保存总览和完整轨迹。

    如果任一写入失败，整个事务回滚。
    """
    package_sql = text(
        """
        insert into public.packages (
            tracking_number,
            carrier,
            weight_kg,
            package_count,
            destination,
            latest_status,
            last_checked_at,
            created_at,
            updated_at
        )
        values (
            :tracking_number,
            :carrier,
            :weight_kg,
            :package_count,
            :destination,
            :latest_status,
            now(),
            now(),
            now()
        )
        on conflict (tracking_number)
        do update set
            carrier =
                excluded.carrier,
            weight_kg =
                excluded.weight_kg,
            package_count =
                excluded.package_count,
            destination =
                excluded.destination,
            latest_status =
                excluded.latest_status,
            last_checked_at =
                now(),
            updated_at =
                now()
        """
    )

    delete_events_sql = text(
        """
        delete from public.tracking_events
        where tracking_number = :tracking_number
        """
    )

    insert_event_sql = text(
        """
        insert into public.tracking_events (
            tracking_number,
            event_order,
            event_date,
            event_time,
            location,
            status
        )
        values (
            :tracking_number,
            :event_order,
            :event_date,
            :event_time,
            :location,
            :status
        )
        """
    )

    package_parameters = {
        "tracking_number":
            tracking_number,
        "carrier":
            carrier,
        "weight_kg":
            weight_kg,
        "package_count":
            package_count,
        "destination":
            destination,
        "latest_status":
            latest_status,
    }

    event_parameters = [
        {
            "tracking_number":
                tracking_number,
            "event_order":
                event_order,
            "event_date":
                event.get("日期", ""),
            "event_time":
                event.get("时间", ""),
            "location":
                event.get("处理地点", ""),
            "status":
                event.get("物流状态", ""),
        }
        for event_order, event
        in enumerate(timeline)
    ]

    with engine.begin() as connection:
        connection.execute(
            package_sql,
            package_parameters,
        )

        connection.execute(
            delete_events_sql,
            {
                "tracking_number":
                    tracking_number,
            },
        )

        if event_parameters:
            connection.execute(
                insert_event_sql,
                event_parameters,
            )


def delete_package(
    engine: Engine,
    tracking_number: str,
) -> bool:
    """删除包裹；轨迹通过外键级联删除。"""
    query = text(
        """
        delete from public.packages
        where tracking_number = :tracking_number
        """
    )

    with engine.begin() as connection:
        result = connection.execute(
            query,
            {
                "tracking_number":
                    tracking_number,
            },
        )

    return result.rowcount > 0


def try_acquire_global_refresh_lock(
    engine: Engine,
    *,
    force: bool,
    cooldown_minutes: int = 10,
    lock_minutes: int = 30,
) -> bool:
    """
    尝试取得全局刷新锁。

    force=False：
    最近 cooldown_minutes 分钟内刷新过则不再刷新。

    force=True：
    忽略冷却时间，但仍阻止两个用户同时刷新。
    """
    query = text(
        """
        update public.app_state
        set
            refresh_locked_until =
                now()
                + make_interval(
                    mins => :lock_minutes
                ),
            updated_at = now()
        where state_key = 'global_refresh'

          and (
                :force = true
                or last_refresh_at is null
                or last_refresh_at <=
                   now()
                   - make_interval(
                       mins =>
                           :cooldown_minutes
                     )
              )

          and (
                refresh_locked_until is null
                or refresh_locked_until < now()
              )

        returning state_key
        """
    )

    with engine.begin() as connection:
        acquired = connection.execute(
            query,
            {
                "force": force,
                "cooldown_minutes":
                    cooldown_minutes,
                "lock_minutes":
                    lock_minutes,
            },
        ).scalar()

    return acquired is not None


def finish_global_refresh(
    engine: Engine,
) -> None:
    """刷新完成，记录时间并解除锁。"""
    query = text(
        """
        update public.app_state
        set
            last_refresh_at = now(),
            refresh_locked_until = null,
            updated_at = now()
        where state_key = 'global_refresh'
        """
    )

    with engine.begin() as connection:
        connection.execute(query)


def release_global_refresh_lock(
    engine: Engine,
) -> None:
    """刷新异常时解除锁，但不更新成功时间。"""
    query = text(
        """
        update public.app_state
        set
            refresh_locked_until = null,
            updated_at = now()
        where state_key = 'global_refresh'
        """
    )

    with engine.begin() as connection:
        connection.execute(query)