"""Extractor module — clicks Historical Usage tab and scrapes the table."""

import logging
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from modules.airnet.utils import retry

logger = logging.getLogger("airnet-scraper")

# Tab selector
_HISTORICAL_TAB = "#dashBoardActionForm\\:historicalUsage_lbl"

# Scope selectors เฉพาะตาราง Historical Usage (ID มีคำว่า beanResult)
# เพื่อป้องกันการดึง headers และ rows จากตารางอื่นบนหน้าเดียวกัน (เช่น Dashboard info table)
_HIST_TABLE     = "table[id*='beanResult']"
_HEADER_CELLS   = "table[id*='beanResult'] thead tr.rich-table-subheader th.rich-table-subheadercell"
_DATA_ROWS      = "table[id*='beanResult'] tbody tr.rich-table-row"
_DATA_CELLS     = "td.rich-table-cell"


@retry(max_attempts=3, delay=2.0)
async def extract_table(page: Page, max_rows: int | None = None) -> list[dict[str, str]]:
    """
    Click the Historical Usage tab and extract up to `max_rows` rows (or all rows if None).

    หมายเหตุ: ใช้ selector ที่ scoped ด้วย table[id*='beanResult'] เพื่อป้องกัน
    การดึง headers/rows จากตารางอื่นที่อยู่บนหน้าเดียวกัน

    Args:
        page: Playwright Page object (must already have a query result loaded).
        max_rows: Maximum number of data rows to extract. Default is None (extract all).

    Returns:
        List of dicts, each representing one row with column header as keys.
        Example:
            [
                {
                    "Customer ID": "88XXXXxxx4",
                    "Service": "INTERNET",
                    ...
                }
            ]

    Raises:
        PlaywrightTimeout: If the table header is not found after clicking the tab.
        RuntimeError: If no headers or no data rows are found in the table.
    """
    logger.info("คลิก tab Historical Usage")
    await page.click(_HISTORICAL_TAB)

    logger.info("รอ 3 วินาทีหลังกด tab...")
    await page.wait_for_timeout(3000)

    # รอ header row ของตาราง Historical Usage โดยเฉพาะ
    try:
        logger.info("รอ thead ของ Historical Usage table โหลด...")
        await page.wait_for_selector(_HEADER_CELLS, timeout=30_000)
    except PlaywrightTimeout as e:
        raise PlaywrightTimeout(
            "ไม่พบ header ของตาราง Historical Usage หลัง 30 วินาที"
        ) from e

    # ดึง column headers เฉพาะตาราง Historical Usage
    logger.info("ดึง headers ของตาราง Historical Usage")
    header_elements = await page.query_selector_all(_HEADER_CELLS)
    headers: list[str] = []
    for el in header_elements:
        text = (await el.inner_text()).strip()
        if not text:
            text = (await el.text_content() or "").strip()
        if text:
            headers.append(text)

    if not headers:
        raise RuntimeError("ไม่พบ header ของตาราง — กรุณาตรวจสอบ selector")

    # จำกัดเฉพาะ 14 columns แรกของ Historical Usage
    headers = headers[:14]
    logger.info("ใช้ %d columns: %s", len(headers), headers)

    # ดึงแถวข้อมูลจาก tbody ของตาราง Historical Usage
    row_elements = await page.query_selector_all(_DATA_ROWS)

    if not row_elements:
        raise RuntimeError("ไม่พบแถวข้อมูลในตาราง Historical Usage")

    if max_rows is not None and max_rows > 0:
        row_elements = row_elements[:max_rows]
        logger.info("พบ %d แถวข้อมูล (จำกัดที่ %d)", len(row_elements), max_rows)
    else:
        logger.info("พบ %d แถวข้อมูล (ดึงทั้งหมด)", len(row_elements))

    result: list[dict[str, str]] = []

    for row_index, row in enumerate(row_elements):
        # ดึงแต่ละ <td> ตรงๆ
        cell_elements = await row.query_selector_all(_DATA_CELLS)
        cell_values: list[str] = []

        for cell in cell_elements[:14]:
            val = (await cell.inner_text()).strip()
            # ถ้า inner_text ว่าง ให้ fallback เป็น text_content หรือ input value
            if not val:
                val = (await cell.text_content() or "").strip()
            if not val:
                input_elem = await cell.query_selector("input")
                if input_elem:
                    val = (await input_elem.get_attribute("value") or "").strip()
            cell_values.append(val)

        # จับคู่ header ↔ cell value (เฉพาะ 14 columns)
        row_data: dict[str, str] = {
            header: cell_values[i] if i < len(cell_values) else ""
            for i, header in enumerate(headers)
        }

        result.append(row_data)
        logger.debug("แถวที่ %d/%d: %s", row_index + 1, len(row_elements), row_data)

    logger.info("ดึงข้อมูลสำเร็จ — %d แถว, %d columns", len(result), len(headers))
    return result
