"""Amazon Seller Central selector candidates.

Selectors are kept in one place so a DOM change normally requires updating
this registry instead of searching the whole workflow for string literals.
The order of each tuple is significant: earlier candidates are preferred.
"""

from typing import Mapping


SELECTORS: Mapping[str, tuple[str, ...]] = {
    # Orders page
    "order_rows": ("tr",),
    "order_status": (".order-status-column",),
    "order_refund_status": (".secondary-status.refund-is-applied", "span[class*='refund']"),
    "order_buyer_link": ('a[data-test-id="buyer-name-with-link"]',),
    "order_buyer_fallback_link": (
        'a:not([href]), a[href=""], a[href="#"], a[href^="javascript:"]',
    ),
    "orders_page_size_select": ("#myo-table-results-per-page",),
    "orders_page_size_prompt": (".a-dropdown-prompt",),
    "orders_page_size_dropdown": (".myo-dropdown",),
    "orders_page_size_100_option": ("#myo-table-results-per-page_3",),
    "orders_page_size_options": ("[id^='myo-table-results-per-page_']", "xpath=//li | //a"),
    "orders_total_heading": (".total-orders-heading",),
    # Contact page
    "file_input": (
        "input[type='file']",
        "input[name*='attachment']",
        "input[name*='file']",
        "input[accept*='pdf']",
    ),
    "attachment_button": (
        "button:has-text('Attach')",
        "button:has-text('Add attachment')",
        "button:has-text('附件')",
        "button:has-text('添加附件')",
        "a:has-text('Attach')",
        "a:has-text('附件')",
    ),
    "send_button": (
        'kat-button[label="发送"]',
        'kat-button[label="Send"]',
        "button:has-text('发送')",
        "button:has-text('Send')",
        "input[type='submit'][value*='发送']",
        "input[type='submit'][value*='Send']",
    ),
    "contact_buyer_prompt": (".navigate-to-contact-buyer",),
    "contact_buyer_navigation": (
        '.navigate-to-contact-buyer kat-button.navigate-button[label="联系买家"]',
    ),
    "message_input": (
        "#case-message-composer .editor textarea.editor-textarea",
        "#case-message-composer .case-message-view-composer-textarea-container.expanded textarea",
        "div.case-message-view-composer-textarea-container textarea:visible",
        ".case-message-view-composer textarea:visible",
        ".case-message-view textarea:visible",
        ".case-message-view-composer [contenteditable='true']:visible",
        ".case-message-view-composer [role='textbox']:visible",
    ),
    "sent_message_body": (".case-message-view .sent-message-component .message-body-text",),
    # Messaging page. Keep these scoped to the message search component so the
    # persistent global navigation search cannot capture an order number.
    "message_search_input": (
        "kat-input[placeholder*='订单编号'] >> input",
        "input[placeholder*='订单编号']",
        "kat-input[placeholder*='电子邮件地址'] >> input",
        "input[placeholder*='电子邮件地址']",
        "[unique-id='katal-id-0'] >> input",
        "[unique-id='katal-id-0']",
    ),
    "message_search_button": (
        'kat-input-group kat-link[variant="button"]',
        'div.case-top-bar kat-link[variant="button"]',
    ),
    "message_item": (
        "a.case-list-item[data-ph-capture-attribute-order-id=\"{order_id}\"]",
        "a.case-list-item:has-text(\"{order_id}\")",
    ),
    "message_list_item": (".case-list-item",),
    "message_subject": (".subject",),
    "conversation_panel": (".case-message-view",),
    "loading_overlay": (".ayb-loading-overlay",),
}


def get_selectors(name: str) -> tuple[str, ...]:
    """Return the ordered selector candidates registered under ``name``."""
    try:
        return SELECTORS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown Amazon selector group: {name}") from exc


def format_selectors(name: str, **values: object) -> tuple[str, ...]:
    """Format a selector group containing placeholders such as ``{order_id}``."""
    return tuple(selector.format(**values) for selector in get_selectors(name))
