import datetime
import io
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

from database import (
    create_db_engine,
    delete_package as delete_package_from_db,
    finish_global_refresh,
    load_all_tracking_events,
    load_package_targets,
    load_packages,
    load_tracking_events,
    package_exists,
    release_global_refresh_lock,
    save_tracking_result,
    try_acquire_global_refresh_lock,
)


st.set_page_config(page_title="Logistic Tracker", layout="wide")

COLUMNS = ["单号", "物流公司", "重量(kg)", "包裹件数", "发往仓库", "最新物流状态", "更新时间"]

STATE_NOTICE = "overview_notice"
STATE_REFRESH_FAILURES = "refresh_failures"
STATE_INITIAL_REFRESH_DONE = "initial_refresh_done"
STATE_PAGE_SIZE = "overview_page_size"
STATE_CURRENT_PAGE = "overview_current_page"
STATE_JUMP_PAGE = "overview_jump_page"

PAGE_SIZE_OPTIONS = [10, 20, 50]
DEFAULT_PAGE_SIZE = 20
PAGE_BUTTONS_PER_ROW = 12

AUTO_REFRESH_COOLDOWN_MINUTES = 10
GLOBAL_REFRESH_LOCK_MINUTES = 30
REFRESH_MAX_WORKERS = 5


def require_secret(name):
    """读取必需的 Streamlit Secret；缺失时停止应用。"""
    try:
        value = st.secrets[name]
    except (KeyError, FileNotFoundError):
        st.error(f"缺少应用密钥：{name}")
        st.stop()

    value = str(value).strip()
    if not value:
        st.error(f"应用密钥为空：{name}")
        st.stop()

    return value


UPS_key = require_secret("UPS_KEY")
UPS_secret = require_secret("UPS_SECRET")
Fedex_key = require_secret("FEDEX_KEY")
Fedex_secret = require_secret("FEDEX_SECRET")
DHL_key = require_secret("DHL_KEY")


@st.cache_resource
def get_database_engine():
    """创建并复用 SQLAlchemy 数据库连接池。"""
    return create_db_engine(require_secret("DATABASE_URL"))

def to_number(value):
    """把 API 或界面数据中的数字字符串转换为 float；无法转换时返回 None。"""
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    match = re.search(r'-?\d+(?:\.\d+)?', str(value).replace(',', '').strip())
    if not match:
        return None

    try:
        return float(match.group())
    except ValueError:
        return None


def convert_weight_to_kg(value, unit=None):
    """把重量转换为千克数值。返回值只包含数字，不附带单位。"""
    number = to_number(value)
    if number is None:
        return None

    normalized_unit = str(unit or '').strip().upper().replace('.', '')

    if normalized_unit in {'KG', 'KGS', 'KILOGRAM', 'KILOGRAMS'}:
        kg = number
    elif normalized_unit in {'LB', 'LBS', 'POUND', 'POUNDS'}:
        kg = number * 0.45359237
    elif normalized_unit in {'G', 'GRAM', 'GRAMS'}:
        kg = number / 1000
    else:
        # 没有可靠单位时不猜测，避免把非千克数据写入“重量(kg)”。
        return None

    return round(kg, 3)


def normalize_piece_count(value):
    """把包裹件数转换为非负整数；无法转换时返回 None。"""
    number = to_number(value)
    if number is None or number < 0 or not float(number).is_integer():
        return None
    return int(number)


def safe_filename(text):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(text))[:80]



def split_iso_datetime(value):
    """把 ISO 时间或普通字符串拆成日期和时间；解析失败时尽量原样返回。"""
    if not value:
        return "", ""

    value = str(value)
    if "T" in value:
        date_part, time_part = value.split("T", 1)
        time_part = time_part.replace("Z", "")
        time_part = time_part.split("+")[0].split("-")[0]
        return date_part, time_part[:8]

    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}", ""

    return value, ""


NO_LOCATION_TEXT = "暂无位置信息"
DHL_NO_DELIVERY_LOCATION_TEXT = "DHL API 未返回签收地点"


def _clean_location_part(value):
    """清理地址字段，过滤空值和无意义占位符。"""
    if value is None:
        return ""

    if isinstance(value, (dict, list, tuple, set)):
        return ""

    value = str(value).strip()
    if not value or value.lower() in {"none", "null", "n/a", "na", "unknown"}:
        return ""
    return value


def _unique_location_parts(parts):
    """保持原顺序去重，避免 city/state/country 重复显示。"""
    result = []
    seen = set()
    for part in parts:
        part = _clean_location_part(part)
        if not part:
            continue
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return result


def format_location(address, _depth=0):
    """
    把不同承运商返回的位置字段统一为可读字符串。

    DHL Unified API 常用 addressLocality，而 UPS/FedEx 更常用 city；
    同时兼容 address/place/location/serviceArea 等嵌套层级。
    """
    if not address or _depth > 4:
        return NO_LOCATION_TEXT

    if isinstance(address, str):
        value = _clean_location_part(address)
        return value or NO_LOCATION_TEXT

    if isinstance(address, (list, tuple)):
        for item in address:
            formatted = format_location(item, _depth + 1)
            if formatted != NO_LOCATION_TEXT:
                return formatted
        return NO_LOCATION_TEXT

    if not isinstance(address, dict):
        value = _clean_location_part(address)
        return value or NO_LOCATION_TEXT

    # 先解析当前对象的标准地址字段。DHL 的城市字段通常是 addressLocality。
    city = (
        address.get('addressLocality')
        or address.get('city')
        or address.get('cityName')
        or address.get('municipality')
        or address.get('locality')
        or address.get('town')
        or address.get('district')
        or ""
    )
    state = (
        address.get('stateOrProvinceCode')
        or address.get('stateOrProvinceName')
        or address.get('administrativeArea')
        or address.get('state')
        or address.get('province')
        or ""
    )
    postal_code = (
        address.get('postalCode')
        or address.get('postcode')
        or address.get('zipCode')
        or ""
    )
    country = (
        address.get('countryCode')
        or address.get('country')
        or address.get('countryName')
        or ""
    )

    parts = _unique_location_parts([city, state, postal_code, country])
    if parts:
        return ", ".join(parts)

    # 某些 DHL 服务把位置名称放在 label/name/description/serviceAreaDescription。
    label = (
        address.get('serviceAreaDescription')
        or address.get('facilityName')
        or address.get('locationName')
        or address.get('label')
        or address.get('description')
        or ""
    )
    label = _clean_location_part(label)
    if label:
        return label

    # Unified API 覆盖多个 DHL 业务，位置对象的嵌套层级并不总是一致。
    for key in (
        'address',
        'place',
        'location',
        'serviceArea',
        'deliveryLocation',
        'destination',
        'destinationAddress',
        'receiver',
        'consignee',
    ):
        nested = address.get(key)
        if not nested or nested is address:
            continue
        formatted = format_location(nested, _depth + 1)
        if formatted != NO_LOCATION_TEXT:
            return formatted

    return NO_LOCATION_TEXT


def location_is_missing(value):
    """判断位置文本是否为空或属于占位提示。"""
    value = str(value or "").strip()
    return value in {"", "N/A", NO_LOCATION_TEXT, DHL_NO_DELIVERY_LOCATION_TEXT}


def _extract_dhl_location_hint(description):
    """从 DHL 事件描述中提取配送站/分拣中心城市，作为缺失位置的保守回退。"""
    text = _clean_location_part(description)
    if not text:
        return ""

    patterns = [
        r"DHL Delivery Facility\s+(.+?)(?:\s+-\s+.+)?$",
        r"DHL Sort Facility\s+(.+?)(?:\s+-\s+.+)?$",
        r"Processed at\s+(.+?)(?:\s+-\s+.+)?$",
        r"departed from a DHL facility\s+(.+?)(?:\s+-\s+.+)?$",
        r"Arrived at\s+(.+?)(?:\s+-\s+.+)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            hint = re.sub(r"\s+", " ", match.group(1)).strip(" ,-.")
            if hint and hint.lower() not in {"your region", "destination"}:
                return hint
    return ""


def _is_country_only(location):
    """识别 NL/CN 等只有国家代码的地址。"""
    return bool(re.fullmatch(r"[A-Z]{2,3}", str(location or "").strip()))


def _merge_location_hint(location, hint):
    """合并两个互补的位置文本，避免 HANGZHOU, CN, CN 一类重复。"""
    location = str(location or "").strip()
    hint = str(hint or "").strip()
    if not hint:
        return location
    if location_is_missing(location):
        return hint
    if location.casefold() == hint.casefold():
        return location

    if _is_country_only(location):
        # hint 已经包含该国家代码时，直接采用信息更完整的 hint。
        if re.search(rf"(?:^|,\s*){re.escape(location)}$", hint, flags=re.IGNORECASE):
            return hint
        return f"{hint}, {location}"

    if _is_country_only(hint):
        if re.search(rf"(?:^|,\s*){re.escape(hint)}$", location, flags=re.IGNORECASE):
            return location
        return f"{location}, {hint}"

    # 例如 location=ROTTERDAM，hint=ROTTERDAM, NL，采用更完整的 hint。
    if location.casefold() in hint.casefold() and len(hint) > len(location):
        return hint
    if hint.casefold() in location.casefold():
        return location
    return location


def extract_dhl_event_location(event):
    """兼容 DHL 不同业务线的事件位置字段。"""
    if not isinstance(event, dict):
        return NO_LOCATION_TEXT

    location = NO_LOCATION_TEXT
    candidates = [
        event.get('location'),
        event.get('place'),
        event.get('address'),
        event.get('serviceArea'),
        event.get('deliveryLocation'),
        event.get('checkpointLocation'),
    ]
    for candidate in candidates:
        formatted = format_location(candidate)
        if not location_is_missing(formatted):
            location = formatted
            break

    hint = _extract_dhl_location_hint(
        event.get('description')
        or event.get('remark')
        or event.get('status')
        or ""
    )
    location = _merge_location_hint(location, hint)
    return location if not location_is_missing(location) else NO_LOCATION_TEXT


def extract_dhl_shipment_destination(shipment):
    """从 DHL 货件级对象中提取目的地，兼容多种嵌套结构。"""
    if not isinstance(shipment, dict):
        return NO_LOCATION_TEXT

    details = shipment.get('details', {})
    status_obj = shipment.get('status', {})
    candidates = [
        shipment.get('destination'),
        shipment.get('destinationAddress'),
        shipment.get('receiver'),
        shipment.get('consignee'),
        shipment.get('deliveryLocation'),
        status_obj.get('location') if isinstance(status_obj, dict) else None,
        details.get('destination') if isinstance(details, dict) else None,
        details.get('destinationAddress') if isinstance(details, dict) else None,
        details.get('deliveryAddress') if isinstance(details, dict) else None,
    ]
    for candidate in candidates:
        formatted = format_location(candidate)
        if not location_is_missing(formatted):
            return formatted
    return NO_LOCATION_TEXT


def normalize_status(status):
    status = str(status or "未知状态")
    if "delivered" in status.lower() or "已签收" in status:
        return "已签收 / Delivered"
    return status


def extract_weight_kg(weight_node):
    """从重量对象或重量数组中提取公斤数值，优先选择明确标记为 KG 的项目。"""
    if isinstance(weight_node, list):
        # 优先读取 API 直接返回的 KG 项。
        for item in weight_node:
            if not isinstance(item, dict):
                continue
            unit = item.get('unit') or item.get('units') or item.get('unitOfMeasurement')
            if str(unit or '').strip().upper() in {'KG', 'KGS', 'KILOGRAM', 'KILOGRAMS'}:
                value = item.get('value') or item.get('weight') or item.get('amount')
                return convert_weight_to_kg(value, unit)

        # 若没有 KG，再尝试可可靠换算的其他单位。
        for item in weight_node:
            value = extract_weight_kg(item)
            if value is not None:
                return value
        return None

    if isinstance(weight_node, dict):
        value = (
            weight_node.get('value')
            or weight_node.get('weight')
            or weight_node.get('amount')
        )
        unit = (
            weight_node.get('unit')
            or weight_node.get('units')
            or weight_node.get('unitOfMeasurement')
        )
        if value is not None:
            return convert_weight_to_kg(value, unit)

    return None


def get_nested_value(obj, path):
    """按字段路径安全读取嵌套字典。"""
    node = obj
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def extract_weight_kg_from_anywhere(obj):
    """从不同承运商可能使用的常见路径提取公斤重量。"""
    if not isinstance(obj, dict):
        return None

    candidate_paths = [
        ['shipmentDetails', 'weight'],
        ['packageDetails', 'weightAndDimensions', 'weight'],
        ['packageDetails', 'weight'],
        ['details', 'totalWeight'],
        ['details', 'weight'],
        ['weight'],
    ]

    for path in candidate_paths:
        weight = extract_weight_kg(get_nested_value(obj, path))
        if weight is not None:
            return weight

    return None

@st.cache_data(ttl=3000, show_spinner=False)
def get_ups_access_token():
    """获取 UPS OAuth Token，并在有效期内缓存。"""
    url = "https://onlinetools.ups.com/security/v1/oauth/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}
    try:
        response = requests.post(
            url,
            headers=headers,
            data=data,
            auth=(UPS_key, UPS_secret),
            timeout=20,
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        return None
    except requests.RequestException:
        return None


@st.cache_data(ttl=3000, show_spinner=False)
def get_fedex_access_token():
    """获取 FedEx OAuth Token，并在有效期内缓存。"""
    url = "https://apis.fedex.com/oauth/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": Fedex_key,
        "client_secret": Fedex_secret,
    }
    try:
        response = requests.post(url, headers=headers, data=data, timeout=20)
        if response.status_code == 200:
            return response.json().get("access_token")
        return None
    except requests.RequestException:
        return None

# ==========================================
# 核心查询逻辑（解析 Overview 信息 + 完整 Detail 历史）
# ==========================================

def track_ups(tracking_number, now_time, access_token=None):
    weight_kg = None
    package_count = None
    destination = "N/A"
    details_timeline = []

    token = access_token or get_ups_access_token()
    if not token:
        return weight_kg, package_count, destination, "凭证错误或网络异常", now_time, details_timeline

    url = f"https://onlinetools.ups.com/api/track/v1/details/{tracking_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "transId": "internal_tracker_req",
        "transactionSrc": "testing"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            data = response.json()
            try:
                shipments = data.get('trackResponse', {}).get('shipment', [])
                shipment = shipments[0] if shipments else {}
                packages = shipment.get('package', [])
                package = packages[0] if packages else {}

                # UPS JSON：package[0].weight.weight + unitOfMeasurement。
                weight_obj = package.get('weight', {})
                weight_kg = convert_weight_to_kg(
                    weight_obj.get('weight'),
                    weight_obj.get('unitOfMeasurement')
                )

                # UPS JSON：package[0].packageCount。
                package_count = normalize_piece_count(package.get('packageCount'))
                if package_count is None and packages:
                    package_count = len(packages)

                # 提取发往仓库
                for addr_node in package.get('packageAddress', []):
                    if addr_node.get('type') == 'DESTINATION':
                        addr_detail = addr_node.get('address', {})
                        destination = format_location(addr_detail)
                        break

                # 遍历 activity 解析完整历史轨迹
                activities = package.get('activity', [])
                for act in activities:
                    raw_date = act.get('date', '')
                    raw_time = act.get('time', '')
                    fmt_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}" if len(raw_date) == 8 else raw_date
                    fmt_time = f"{raw_time[:2]}:{raw_time[2:4]}:{raw_time[4:6]}" if len(raw_time) >= 6 else raw_time

                    loc_obj = act.get('location', {}).get('address', {})
                    loc_str = format_location(loc_obj)
                    desc = act.get('status', {}).get('description', '未知状态')

                    details_timeline.append({
                        "日期": fmt_date,
                        "时间": fmt_time,
                        "处理地点": loc_str,
                        "物流状态": desc
                    })

                if activities:
                    status = activities[0].get('status', {}).get('description', '状态解析中')
                    status = normalize_status(status)
                else:
                    status = "无物流动态"

                return weight_kg, package_count, destination, status, now_time, details_timeline

            except Exception:
                return weight_kg, package_count, destination, "部分字段解析异常", now_time, details_timeline
        else:
            return weight_kg, package_count, destination, f"查询失败：HTTP {response.status_code}", now_time, details_timeline
    except Exception:
        return weight_kg, package_count, destination, "网络连接失败", now_time, details_timeline

def track_dhl(tracking_number, now_time):
    """
    DHL Shipment Tracking - Unified API。

    重点：DHL Unified API 覆盖 Express、eCommerce、Parcel、Freight 等服务，
    各服务返回的位置字段可能不同，因此这里采用多路径解析，并在签收事件缺少
    城市时使用货件目的地或最近一次有位置的配送事件作回退。
    """
    weight_kg = None
    package_count = None
    destination = NO_LOCATION_TEXT
    details_timeline = []

    url = "https://api-eu.dhl.com/track/shipments"
    headers = {
        "Accept": "application/json",
        "DHL-API-Key": DHL_key,
    }
    params = {
        "trackingNumber": str(tracking_number).strip(),
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        try:
            data = response.json()
        except Exception:
            return weight_kg, package_count, destination, "DHL返回非JSON数据", now_time, details_timeline

        if response.status_code != 200:
            return weight_kg, package_count, destination, f"DHL查询失败：HTTP {response.status_code}", now_time, details_timeline

        shipments = data.get('shipments', [])
        if not shipments:
            return weight_kg, package_count, destination, "DHL无匹配物流信息", now_time, details_timeline

        shipment = shipments[0]
        status_obj = shipment.get('status', {})
        status = (
            status_obj.get('description')
            or status_obj.get('status')
            or status_obj.get('statusCode')
            or shipment.get('description')
            or "状态解析中"
        )
        status = normalize_status(status)

        # 先解析货件级目的地；DHL 常见字段包括 destination.address.addressLocality。
        shipment_destination = extract_dhl_shipment_destination(shipment)
        if not location_is_missing(shipment_destination):
            destination = shipment_destination

        details_obj = shipment.get('details', {})
        if isinstance(details_obj, dict):
            package_count = normalize_piece_count(details_obj.get('totalNumberOfPieces'))
            if package_count is None:
                piece_ids = details_obj.get('pieceIds', [])
                if isinstance(piece_ids, list) and piece_ids:
                    package_count = len(piece_ids)

        weight_kg = extract_weight_kg_from_anywhere(shipment)

        events = shipment.get('events', [])
        if not events and isinstance(details_obj, dict):
            events = details_obj.get('events', [])
        if not isinstance(events, list):
            events = []

        # 预先解析每条事件的位置与描述，便于签收事件向较早事件回退。
        parsed_events = []
        for event in events:
            if not isinstance(event, dict):
                continue

            timestamp = event.get('timestamp') or event.get('date') or event.get('time') or ""
            fmt_date, fmt_time = split_iso_datetime(timestamp)
            desc = (
                event.get('description')
                or event.get('status')
                or event.get('remark')
                or event.get('type')
                or "未知状态"
            )
            location = extract_dhl_event_location(event)
            parsed_events.append({
                "日期": fmt_date,
                "时间": fmt_time,
                "处理地点": location,
                "物流状态": str(desc),
            })

        delivered_location = NO_LOCATION_TEXT
        for index, record in enumerate(parsed_events):
            description = record["物流状态"]
            is_delivered = "delivered" in description.lower() or "已签收" in description
            if not is_delivered:
                continue

            location = record["处理地点"]

            # 若签收事件只有国家代码或没有位置，优先借用较早的派送/配送站事件城市。
            for older_record in parsed_events[index + 1:]:
                older_location = older_record["处理地点"]
                if location_is_missing(older_location):
                    continue
                location = _merge_location_hint(location, older_location)
                if not location_is_missing(location) and not _is_country_only(location):
                    break

            # 再用货件级目的地补足国家/城市，例如 ROTTERDAM + ROTTERDAM, NL。
            if not location_is_missing(shipment_destination):
                location = _merge_location_hint(location, shipment_destination)

            if location_is_missing(location):
                location = DHL_NO_DELIVERY_LOCATION_TEXT

            record["处理地点"] = location
            if location != DHL_NO_DELIVERY_LOCATION_TEXT and location_is_missing(delivered_location):
                delivered_location = location

        details_timeline = parsed_events

        # 已签收包裹的 Overview 优先显示签收地点；否则显示货件目的地。
        if status == "已签收 / Delivered" and not location_is_missing(delivered_location):
            destination = delivered_location
        elif location_is_missing(destination):
            # 若货件级目的地缺失，使用最新一条可用事件位置。
            for record in details_timeline:
                if not location_is_missing(record["处理地点"]):
                    destination = record["处理地点"]
                    break

        if location_is_missing(destination):
            destination = DHL_NO_DELIVERY_LOCATION_TEXT if status == "已签收 / Delivered" else NO_LOCATION_TEXT

        return weight_kg, package_count, destination, status, now_time, details_timeline

    except Exception:
        return weight_kg, package_count, destination, "DHL网络连接失败", now_time, details_timeline

def track_fedex(tracking_number, now_time, access_token=None):
    """FedEx Track API：解析公斤重量、包裹件数和完整扫描记录。"""
    weight_kg = None
    package_count = None
    destination = "N/A"
    details_timeline = []

    token = access_token or get_fedex_access_token()
    if not token:
        return weight_kg, package_count, destination, "FedEx凭证错误或网络异常", now_time, details_timeline

    url = "https://apis.fedex.com/track/v1/trackingnumbers"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-locale": "zh_CN",
    }
    payload = {
        "includeDetailedScans": True,
        "trackingInfo": [
            {
                "trackingNumberInfo": {
                    "trackingNumber": str(tracking_number).strip()
                }
            }
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        try:
            data = response.json()
        except Exception:
            return weight_kg, package_count, destination, "FedEx返回非JSON数据", now_time, details_timeline

        if response.status_code != 200:
            return weight_kg, package_count, destination, f"FedEx查询失败：HTTP {response.status_code}", now_time, details_timeline

        complete_results = data.get('output', {}).get('completeTrackResults', [])
        if not complete_results:
            return weight_kg, package_count, destination, "FedEx无匹配物流信息", now_time, details_timeline

        track_results = complete_results[0].get('trackResults', [])
        if not track_results:
            return weight_kg, package_count, destination, "FedEx无物流详情", now_time, details_timeline

        result = track_results[0]
        if result.get('error'):
            error = result.get('error', {})
            msg = error.get('message') or error.get('code') or "FedEx返回错误"
            return weight_kg, package_count, destination, msg, now_time, details_timeline

        status_obj = result.get('latestStatusDetail', {})
        status = (
            status_obj.get('description')
            or status_obj.get('statusByLocale')
            or status_obj.get('derivedStatus')
            or status_obj.get('code')
            or "状态解析中"
        )
        status = normalize_status(status)

        destination_obj = (
            result.get('lastUpdatedDestinationAddress')
            or result.get('recipientInformation', {}).get('address')
            or result.get('destinationLocation', {}).get('locationContactAndAddress', {}).get('address')
            or {}
        )
        destination = format_location(destination_obj)

        # FedEx JSON：优先读取货件级 shipmentDetails.weight 数组中 unit == KG 的 value。
        weight_kg = extract_weight_kg(result.get('shipmentDetails', {}).get('weight'))
        if weight_kg is None:
            weight_kg = extract_weight_kg(
                result.get('packageDetails', {}).get('weightAndDimensions', {}).get('weight')
            )

        # FedEx JSON：packageDetails.count。
        package_count = normalize_piece_count(result.get('packageDetails', {}).get('count'))

        scan_events = result.get('scanEvents', [])
        for event in scan_events:
            fmt_date, fmt_time = split_iso_datetime(event.get('date', ''))

            loc_obj = event.get('scanLocation') or event.get('location') or {}
            loc_str = format_location(loc_obj)

            desc = (
                event.get('eventDescription')
                or event.get('derivedStatus')
                or event.get('exceptionDescription')
                or event.get('eventType')
                or "未知状态"
            )

            details_timeline.append({
                "日期": fmt_date,
                "时间": fmt_time,
                "处理地点": loc_str,
                "物流状态": desc,
            })

        return weight_kg, package_count, destination, status, now_time, details_timeline

    except Exception:
        return weight_kg, package_count, destination, "FedEx网络连接失败", now_time, details_timeline

def track_package(tracking_number, carrier, tokens=None):
    """按承运商查询一个物流单号。"""
    tokens = tokens or {}
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if carrier == "UPS":
        return track_ups(
            tracking_number,
            now_time,
            access_token=tokens.get("UPS"),
        )
    if carrier == "DHL":
        return track_dhl(tracking_number, now_time)
    if carrier == "FedEx":
        return track_fedex(
            tracking_number,
            now_time,
            access_token=tokens.get("FedEx"),
        )

    # 模拟数据：目前顺丰仍未接入真实 API。
    destinations = ["Shenzhen, CN", "Hong Kong, HK", "Hangzhou, CN", "Frankfurt, DE"]
    weight_kg = round(random.uniform(1.0, 50.0), 2)
    package_count = 1
    destination = random.choice(destinations)
    current_status = "[模拟] 运输中"
    mock_timeline = [
        {
            "日期": "2026-03-24",
            "时间": "10:00:00",
            "处理地点": destination,
            "物流状态": "运输中",
        },
        {
            "日期": "2026-03-23",
            "时间": "18:00:00",
            "处理地点": "发件地",
            "物流状态": "已揽收",
        },
    ]
    return weight_kg, package_count, destination, current_status, now_time, mock_timeline


TRACKING_FAILURE_MARKERS = (
    "查询失败",
    "网络连接失败",
    "网络异常",
    "凭证错误",
    "返回非JSON",
    "无匹配物流信息",
    "无物流详情",
    "部分字段解析异常",
)


def tracking_result_failed(status):
    """判断承运商查询结果是否属于失败状态。"""
    status = str(status or "")
    return any(marker in status for marker in TRACKING_FAILURE_MARKERS)


def build_batch_tokens(carriers):
    """一次批量操作只获取一次 UPS/FedEx Token。"""
    carriers = set(carriers)
    tokens = {}

    if "UPS" in carriers:
        tokens["UPS"] = get_ups_access_token()
    if "FedEx" in carriers:
        tokens["FedEx"] = get_fedex_access_token()

    return tokens


def empty_overview_dataframe():
    """创建结构固定的空包裹列表。"""
    return pd.DataFrame(columns=COLUMNS)


def normalize_overview_dataframe(df):
    """统一 Overview 的字段、类型和列顺序。"""
    if df is None:
        return empty_overview_dataframe()

    normalized = df.copy()
    for column in COLUMNS:
        if column not in normalized.columns:
            normalized[column] = pd.NA

    normalized["单号"] = normalized["单号"].astype("string")
    normalized["重量(kg)"] = pd.to_numeric(normalized["重量(kg)"], errors="coerce")
    normalized["包裹件数"] = pd.to_numeric(
        normalized["包裹件数"], errors="coerce"
    ).astype("Int64")

    return normalized[COLUMNS].reset_index(drop=True)


def initialize_session_state():
    """只初始化当前浏览器会话的页面临时状态。"""
    if st.session_state.get(STATE_PAGE_SIZE) not in PAGE_SIZE_OPTIONS:
        st.session_state[STATE_PAGE_SIZE] = DEFAULT_PAGE_SIZE
    if STATE_CURRENT_PAGE not in st.session_state:
        st.session_state[STATE_CURRENT_PAGE] = 1
    if STATE_JUMP_PAGE not in st.session_state:
        st.session_state[STATE_JUMP_PAGE] = 1
    if STATE_INITIAL_REFRESH_DONE not in st.session_state:
        st.session_state[STATE_INITIAL_REFRESH_DONE] = False


def get_packages():
    """始终从 Supabase PostgreSQL 读取包裹总览。"""
    return normalize_overview_dataframe(load_packages(get_database_engine()))


def delete_package(tracking_number):
    """按物流单号删除数据库记录及其轨迹。"""
    tracking_number = str(tracking_number).strip()
    deleted = delete_package_from_db(get_database_engine(), tracking_number)
    return tracking_number if deleted else None


def add_packages(tracking_numbers, carrier):
    """查询并写入尚未存在的物流单号。"""
    engine = get_database_engine()
    tokens = build_batch_tokens({carrier})
    added_count = 0
    failures = []
    seen_numbers = set()

    for tracking_number in tracking_numbers:
        tracking_number = str(tracking_number).strip()
        if not tracking_number or tracking_number in seen_numbers:
            continue
        seen_numbers.add(tracking_number)

        if package_exists(engine, tracking_number):
            continue

        try:
            weight, package_count, destination, status, _update_time, timeline = track_package(
                tracking_number,
                carrier,
                tokens=tokens,
            )

            if tracking_result_failed(status):
                raise RuntimeError(str(status))

            save_tracking_result(
                engine=engine,
                tracking_number=tracking_number,
                carrier=carrier,
                weight_kg=weight,
                package_count=package_count,
                destination=destination,
                latest_status=status,
                timeline=timeline,
            )
            added_count += 1

        except Exception as exc:
            failures.append({"单号": tracking_number, "错误": str(exc)})

    return added_count, failures


def refresh_all_packages(max_workers=REFRESH_MAX_WORKERS):
    """并发刷新数据库中的全部包裹，失败时保留旧数据。"""
    engine = get_database_engine()
    targets = load_package_targets(engine)

    if not targets:
        return 0, 0, []

    tokens = build_batch_tokens({target["carrier"] for target in targets})
    success_count = 0
    failure_count = 0
    failures = []

    def refresh_one(target):
        tracking_number = str(target["tracking_number"]).strip()
        carrier = str(target["carrier"]).strip()
        result = track_package(tracking_number, carrier, tokens=tokens)
        return tracking_number, carrier, result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(refresh_one, target): target
            for target in targets
        }

        for future in as_completed(futures):
            target = futures[future]
            tracking_number = str(target["tracking_number"])

            try:
                returned_number, carrier, result = future.result()
                weight, package_count, destination, status, _update_time, timeline = result

                if tracking_result_failed(status):
                    raise RuntimeError(str(status))

                save_tracking_result(
                    engine=engine,
                    tracking_number=returned_number,
                    carrier=carrier,
                    weight_kg=weight,
                    package_count=package_count,
                    destination=destination,
                    latest_status=status,
                    timeline=timeline,
                )
                success_count += 1

            except Exception as exc:
                failure_count += 1
                failures.append({"单号": tracking_number, "错误": str(exc)})

    return success_count, failure_count, failures


def run_global_refresh(*, force):
    """取得数据库全局锁后执行全量刷新。"""
    engine = get_database_engine()
    acquired = try_acquire_global_refresh_lock(
        engine,
        force=force,
        cooldown_minutes=AUTO_REFRESH_COOLDOWN_MINUTES,
        lock_minutes=GLOBAL_REFRESH_LOCK_MINUTES,
    )

    if not acquired:
        return None

    try:
        result = refresh_all_packages(max_workers=REFRESH_MAX_WORKERS)
        finish_global_refresh(engine)
        return result
    except Exception:
        release_global_refresh_lock(engine)
        raise

def display_weight(value, with_unit=False):
    """格式化重量显示。"""
    if pd.isna(value):
        return "—"
    suffix = " kg" if with_unit else ""
    return f"{float(value):g}{suffix}"


def display_piece_count(value):
    """格式化包裹件数显示。"""
    return "—" if pd.isna(value) else str(int(value))


def calculate_total_pages(total_rows, page_size):
    """计算总页数；空列表也保持为第 1 页。"""
    return max(1, (total_rows + page_size - 1) // page_size)


def clamp_page(page, total_pages):
    """把页码限制在有效范围内。"""
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    return max(1, min(page, total_pages))


def set_current_page(page):
    """分页按钮回调：切换当前页。"""
    st.session_state[STATE_CURRENT_PAGE] = max(1, int(page))


def reset_pagination():
    """每页行数改变时返回第 1 页。"""
    st.session_state[STATE_CURRENT_PAGE] = 1
    st.session_state[STATE_JUMP_PAGE] = 1


def jump_to_selected_page(total_pages):
    """手动页码跳转按钮回调。"""
    selected_page = st.session_state.get(STATE_JUMP_PAGE, 1)
    st.session_state[STATE_CURRENT_PAGE] = clamp_page(selected_page, total_pages)


def paginate_dataframe(df):
    """根据会话中的分页状态返回当前页数据和分页元信息。"""
    page_size = int(st.session_state[STATE_PAGE_SIZE])
    total_pages = calculate_total_pages(len(df), page_size)
    current_page = clamp_page(
        st.session_state.get(STATE_CURRENT_PAGE, 1), total_pages
    )
    st.session_state[STATE_CURRENT_PAGE] = current_page

    jump_page = clamp_page(st.session_state.get(STATE_JUMP_PAGE, 1), total_pages)
    st.session_state[STATE_JUMP_PAGE] = jump_page

    start = (current_page - 1) * page_size
    end = min(start + page_size, len(df))
    return df.iloc[start:end], current_page, total_pages, start, end

@st.dialog("物流历史轨迹详情", width="large")
def show_package_detail(tracking_number):
    """在弹窗中展示指定包裹的基础信息和数据库轨迹。"""
    df = get_packages()
    matched = df[df["单号"].astype(str) == str(tracking_number)]

    if matched.empty:
        st.warning("该包裹已不在当前列表中。")
        return

    row = matched.iloc[0]
    st.subheader(str(tracking_number))

    left, right = st.columns(2)
    left.markdown(f"**承运商：** {row['物流公司']}")
    left.markdown(f"**目的地：** {row['发往仓库']}")
    right.markdown(f"**重量：** {display_weight(row['重量(kg)'], with_unit=True)}")
    right.markdown(f"**包裹件数：** {display_piece_count(row['包裹件数'])}")

    st.markdown(f"**当前状态：** {row['最新物流状态']}")
    st.caption(f"最后更新：{row['更新时间']}")
    st.markdown("---")

    timeline = load_tracking_events(get_database_engine(), str(tracking_number))
    if timeline:
        st.dataframe(pd.DataFrame(timeline), hide_index=True, use_container_width=True)
    else:
        st.info("暂未获取到该单号的历史轨迹记录。")

    if st.button(
        "关闭",
        use_container_width=True,
        key=f"close_{safe_filename(tracking_number)}",
    ):
        st.rerun()

def render_overview_rows(df):
    """以紧凑表格显示当前页包裹；每行提供删除和详情操作。"""
    field_widths = [0.55, 0.55, 1.8, 0.9, 0.8, 0.8, 1.7, 2.5, 1.5]
    headers = [
        "", "", "单号", "物流公司", "重量(kg)", "包裹件数",
        "发往仓库", "最新物流状态", "更新时间",
    ]

    header_columns = st.columns(field_widths, gap="small")
    for column, title in zip(header_columns, headers):
        column.markdown(f"**{title}**" if title else "")

    st.markdown(
        "<hr style='margin: 0.25rem 0 0.45rem 0;'>",
        unsafe_allow_html=True,
    )

    for row_position, (row_index, row) in enumerate(df.iterrows()):
        tracking_number = str(row["单号"]).strip()
        row_columns = st.columns(field_widths, gap="small")
        key_suffix = f"{row_index}_{safe_filename(tracking_number)}"

        if row_columns[0].button(
            "删除",
            key=f"delete_{key_suffix}",
            use_container_width=True,
            help=f"删除单号 {tracking_number} 的整行信息",
        ):
            deleted_number = delete_package(tracking_number)
            if deleted_number:
                st.session_state[STATE_NOTICE] = f"已删除单号 {deleted_number} 的记录。"
            else:
                st.session_state[STATE_NOTICE] = f"未找到单号 {tracking_number} 的记录。"
            st.rerun()

        if row_columns[1].button(
            "详情",
            key=f"detail_{key_suffix}",
            use_container_width=True,
            help=f"查看单号 {tracking_number} 的物流轨迹",
        ):
            show_package_detail(tracking_number)

        row_columns[2].write(tracking_number)
        row_columns[3].write(str(row["物流公司"]))
        row_columns[4].write(display_weight(row["重量(kg)"]))
        row_columns[5].write(display_piece_count(row["包裹件数"]))
        row_columns[6].write(str(row["发往仓库"]))
        row_columns[7].write(str(row["最新物流状态"]))
        row_columns[8].write(str(row["更新时间"]))

        if row_position < len(df) - 1:
            st.markdown(
                "<hr style='margin: 0.15rem 0 0.35rem 0; border: none; "
                "border-top: 1px solid rgba(128,128,128,0.18);'>",
                unsafe_allow_html=True,
            )

def render_page_number_buttons(current_page, total_pages):
    """按实际总页数生成方形页码按钮，并在空间不足时自动换行。"""
    st.markdown(
        """
        <style>
        .st-key-pagination-page-buttons div[data-testid="stButton"] > button {
            width: 2.6rem;
            min-width: 2.6rem;
            height: 2.6rem;
            min-height: 2.6rem;
            padding: 0;
            border-radius: 0.25rem;
        }
        .st-key-pagination-page-buttons div[data-testid="stColumn"] {
            display: flex;
            justify-content: center;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="pagination-page-buttons"):
        for first_page in range(1, total_pages + 1, PAGE_BUTTONS_PER_ROW):
            page_numbers = list(
                range(
                    first_page,
                    min(first_page + PAGE_BUTTONS_PER_ROW, total_pages + 1),
                )
            )
            columns = st.columns(PAGE_BUTTONS_PER_ROW, gap="small")
            for column, page_number in zip(columns, page_numbers):
                column.button(
                    str(page_number),
                    key=f"page_number_{page_number}",
                    type="primary" if page_number == current_page else "secondary",
                    use_container_width=True,
                    on_click=set_current_page,
                    args=(page_number,),
                    help=f"跳转到第 {page_number} 页",
                )


def render_pagination_controls(current_page, total_pages):
    """渲染首尾、前后、手动跳转和页码按钮。"""
    first_column, previous_column, page_column, next_column, last_column, input_column, jump_column = st.columns(
        [1, 1, 1.15, 1, 1, 1.25, 0.8],
        gap="small",
    )

    first_column.button(
        "<<",
        key="pagination_first",
        use_container_width=True,
        disabled=current_page == 1,
        on_click=set_current_page,
        args=(1,),
    )
    previous_column.button(
        "<",
        key="pagination_previous",
        use_container_width=True,
        disabled=current_page == 1,
        on_click=set_current_page,
        args=(current_page - 1,),
    )
    page_column.markdown(
        f"<div style='text-align:center; padding-top:0.55rem;'>"
        f"第 <strong>{current_page}</strong> / {total_pages} 页</div>",
        unsafe_allow_html=True,
    )
    next_column.button(
        ">",
        key="pagination_next",
        use_container_width=True,
        disabled=current_page == total_pages,
        on_click=set_current_page,
        args=(current_page + 1,),
    )
    last_column.button(
        ">>",
        key="pagination_last",
        use_container_width=True,
        disabled=current_page == total_pages,
        on_click=set_current_page,
        args=(total_pages,),
    )
    input_column.number_input(
        "跳转到第 [x] 页",
        min_value=1,
        max_value=total_pages,
        step=1,
        key=STATE_JUMP_PAGE,
        label_visibility="collapsed",
    )
    jump_column.button(
        "跳转",
        key="pagination_jump",
        use_container_width=True,
        on_click=jump_to_selected_page,
        args=(total_pages,),
    )

    render_page_number_buttons(current_page, total_pages)


def generate_excel_bytes(df_overview, details):
    """生成包含 Overview 和逐单详情工作表的 Excel。"""
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_overview.to_excel(writer, sheet_name="overview", index=False)

        for tracking_number in df_overview["单号"].astype(str):
            timeline = details.get(tracking_number, [])
            detail_df = (
                pd.DataFrame(timeline)
                if timeline
                else pd.DataFrame([{"提示": "暂无该包裹的详细轨迹记录"}])
            )

            sheet_name = tracking_number[:31]
            for invalid_character in [":", "/", "[", "]", "*", "?", "\\"]:
                sheet_name = sheet_name.replace(invalid_character, "")
            detail_df.to_excel(
                writer,
                sheet_name=sheet_name or "detail",
                index=False,
            )

    return output.getvalue()

def render_app():
    initialize_session_state()

    st.title("Logistic Tracker")

    notice = st.session_state.pop(STATE_NOTICE, None)
    if notice:
        st.success(notice)

    refresh_failures = st.session_state.pop(STATE_REFRESH_FAILURES, None)
    if refresh_failures:
        with st.expander("查看刷新失败记录"):
            st.dataframe(
                pd.DataFrame(refresh_failures),
                hide_index=True,
                use_container_width=True,
            )

    # 每个新浏览器会话只执行一次自动刷新检查。
    if not st.session_state[STATE_INITIAL_REFRESH_DONE]:
        st.session_state[STATE_INITIAL_REFRESH_DONE] = True
        existing_targets = load_package_targets(get_database_engine())

        if existing_targets:
            with st.spinner(
                f"正在检查 {len(existing_targets)} 个包裹的最新状态……"
            ):
                refresh_result = run_global_refresh(force=False)

            if refresh_result is not None:
                success_count, failure_count, failures = refresh_result
                st.session_state[STATE_NOTICE] = (
                    f"自动查询完成：成功 {success_count} 个，失败 {failure_count} 个。"
                )
                if failures:
                    st.session_state[STATE_REFRESH_FAILURES] = failures
                st.rerun()

    df_packages = get_packages()

    st.markdown("---")
    st.subheader("录入新单号")

    input_column, carrier_column, submit_column = st.columns([2, 1, 1])
    with input_column:
        new_tracking_numbers = st.text_area(
            "输入物流单号（支持批量粘贴，每行一个）",
            height=100,
        )
    with carrier_column:
        carrier = st.selectbox("选择物流服务商", ["UPS", "DHL", "FedEx", "顺丰"])
    with submit_column:
        st.write("")
        st.write("")
        add_clicked = st.button(
            "录入并开始追踪",
            type="primary",
            use_container_width=True,
        )

    if add_clicked:
        numbers = [
            number.strip()
            for number in new_tracking_numbers.splitlines()
            if number.strip()
        ]

        if not numbers:
            st.error("请输入至少一个单号！")
        else:
            with st.spinner("正在查询物流信息……"):
                added_count, add_failures = add_packages(numbers, carrier)

            if added_count:
                df_packages = get_packages()
                total_pages = calculate_total_pages(
                    len(df_packages),
                    int(st.session_state[STATE_PAGE_SIZE]),
                )
                st.session_state[STATE_CURRENT_PAGE] = total_pages
                st.session_state[STATE_JUMP_PAGE] = total_pages
                st.success(f"成功录入 {added_count} 个新包裹！")

            if add_failures:
                st.warning(f"有 {len(add_failures)} 个单号未能成功录入。")
                st.dataframe(
                    pd.DataFrame(add_failures),
                    hide_index=True,
                    use_container_width=True,
                )

            if not added_count and not add_failures:
                st.warning("录入的单号已存在，请勿重复录入。")

    st.markdown("---")

    title_column, refresh_column, export_column = st.columns([3, 1, 1])
    with title_column:
        st.subheader("包裹列表")

    with refresh_column:
        refresh_clicked = st.button("刷新", use_container_width=True)

    if refresh_clicked:
        targets = load_package_targets(get_database_engine())

        if not targets:
            st.info("当前没有需要刷新的包裹。")
        else:
            with st.spinner(f"正在刷新 {len(targets)} 个包裹……"):
                refresh_result = run_global_refresh(force=True)

            if refresh_result is None:
                st.info("另一名用户正在刷新物流，请稍后重新查看。")
            else:
                success_count, failure_count, failures = refresh_result
                st.session_state[STATE_NOTICE] = (
                    f"刷新完成：成功 {success_count} 个，失败 {failure_count} 个。"
                )
                if failures:
                    st.session_state[STATE_REFRESH_FAILURES] = failures
                st.rerun()

    with export_column:
        now = datetime.datetime.now()
        file_name = f"tracker_{now:%Y%m%d_%H%M}.xlsx"
        details = (
            load_all_tracking_events(get_database_engine())
            if not df_packages.empty
            else {}
        )
        st.download_button(
            label="导出所有数据至 Excel",
            data=generate_excel_bytes(df_packages, details),
            file_name=file_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            disabled=df_packages.empty,
        )

    # 录入动作可能已经改变数据库，重新读取一次列表。
    df_packages = get_packages()

    if df_packages.empty:
        st.write("暂无包裹，请在上方录入。")
        return

    summary_column, page_size_column = st.columns([4, 1])
    with summary_column:
        st.caption(f"共 {len(df_packages)} 个包裹")
    with page_size_column:
        st.selectbox(
            "每页显示",
            PAGE_SIZE_OPTIONS,
            key=STATE_PAGE_SIZE,
            format_func=lambda value: f"{value} 行",
            on_change=reset_pagination,
        )

    page_df, current_page, total_pages, start, end = paginate_dataframe(df_packages)
    st.caption(f"当前显示第 {start + 1}–{end} 行，共 {len(df_packages)} 行")
    render_overview_rows(page_df)
    st.markdown("---")
    render_pagination_controls(current_page, total_pages)


if __name__ == "__main__":
    render_app()
