"""Tests for Amazon order parsing functionality."""

import pytest

from ynab_amazon_categorizer.amazon_parser import AmazonParser


def test_parse_simple_order() -> None:
    """Test parsing a simple Amazon order."""
    order_text = """
    Order placed July 31, 2024
    Total $57.57
    Order # 702-8237239-1234567
    Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)
    """

    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    assert len(orders) == 1
    order = orders[0]
    assert order.order_id == "702-8237239-1234567"
    assert order.total == 57.57
    assert order.date_str == "July 31, 2024"
    assert len(order.items) == 1
    assert "Fancy Feast" in order.items[0]


def test_parse_comma_formatted_total() -> None:
    """Totals with thousands separators are parsed as the full amount."""
    order_text = """
    Order placed January 1, 2026
    Total $1,234.56
    Order # 702-8237239-1234567
    Premium Product Name With Enough Words To Parse
    """

    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    assert len(orders) == 1
    assert orders[0].total == 1234.56


def test_parse_empty_order_text() -> None:
    """Test parsing empty order text returns empty list."""
    parser = AmazonParser()
    orders = parser.parse_orders_page("")
    assert len(orders) == 0


def test_extract_items_from_order_content() -> None:
    """Test extracting items from Amazon order content."""
    parser = AmazonParser()

    order_content = """
    Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)
    ACME Brand Premium Dog Treats - Large Size 2 lb bag
    Skip this line
    Buy it again - should be skipped
    Another product: Organic Cat Litter - 20 lbs Natural Clay
    Track package - should be skipped
    """

    items = parser.extract_items_from_content(order_content)

    assert len(items) >= 1
    assert "Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)" in items


def test_parse_order_without_items_kept() -> None:
    """Orders without parseable items are still kept (partial orders)."""
    order_text = """
    Order placed January 5, 2025
    Total $15.00
    Order # 702-1111111-2222222
    short
    """
    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    # Order is kept even without items
    assert len(orders) == 1
    assert orders[0].order_id == "702-1111111-2222222"
    assert orders[0].total == 15.00
    assert orders[0].items == []


def test_parse_multiple_orders() -> None:
    """Multiple orders in same text are parsed correctly."""
    order_text = """
    Order placed July 31, 2024
    Total $57.57
    Order # 702-1111111-1111111
    Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)

    Order placed August 15, 2024
    Total $89.99
    Order # 702-2222222-2222222
    Wireless Bluetooth Headphones - Over-Ear Noise Cancelling
    """

    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    assert len(orders) == 2
    assert orders[0].order_id == "702-1111111-1111111"
    assert orders[0].total == 57.57
    assert orders[1].order_id == "702-2222222-2222222"
    assert orders[1].total == 89.99


def test_extract_more_than_three_items() -> None:
    """More than 3 items can be extracted (old 3-item limit removed)."""
    parser = AmazonParser()

    order_content = """
    Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)
    ACME Brand Premium Dog Treats - Large Size 2 lb bag
    Another product: Organic Cat Litter - 20 lbs Natural Clay
    Super Premium Fancy Dog Collar with LED - Size Medium
    Deluxe Pet Bed Extra Large Orthopedic Memory Foam 36 inch
    """

    items = parser.extract_items_from_content(order_content)
    assert len(items) >= 4  # Should extract more than 3 now


def test_cancelled_order_items_do_not_bleed() -> None:
    """Items from a cancelled order must not appear under the preceding valid order."""
    # Order 701-9742314-2110603 ($29.58 capsules) is followed by a cancelled order
    # whose item (Altra sneaker) must not be attributed to the capsules order.
    order_text = """Order placed
April 22, 2026
Total
$29.58
Ship to
Shelby and Kalman Sutker
Order # 701-9742314-2110603
View order details Invoice

Now arriving today 5:15 p.m. - 8:15 p.m.

    Yogti Psyllium Husk Capsules, 300 Count 2
    Yogti Psyllium Husk Capsules, 300 Count
    Buy it again

    Track package
    Return or replace items
    Write a product review

Order placed
April 22, 2026
Order # 701-0552104-1147435

Cancelled
Your order was cancelled. You have not been charged for this order.

    Altra Men's Lone Peak 9 Trail Running Shoe, Tan, 13 Wide
    Altra Men's Lone Peak 9 Trail Running Shoe, Tan, 13 Wide

Order placed
April 22, 2026
Total
$29.73
Ship to
Shelby and Kalman Sutker
Order # 701-5052171-6951446
View order details Invoice

    TEMPTATIONS Cat Treats, Seafood Medley Flavour, 454g Tub 4
    TEMPTATIONS Cat Treats, Seafood Medley Flavour, 454g Tub
    Buy it again
    View your item
"""

    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    # Cancelled order must be omitted entirely
    assert len(orders) == 2
    order_ids = [o.order_id for o in orders]
    assert "701-0552104-1147435" not in order_ids

    capsules_order = next(o for o in orders if o.order_id == "701-9742314-2110603")
    item_text = " ".join(capsules_order.items)
    assert "Altra" not in item_text, "Cancelled-order item bled into adjacent order"
    assert "Psyllium" in item_text


def test_cancelled_order_detection_is_case_insensitive() -> None:
    """An all-caps 'ORDER PLACED' / 'Your order was cancelled' header/notice
    must still be recognized and excluded, not just the lowercase 'Order
    placed' form.
    """
    order_text = """ORDER PLACED
June 1, 2026
TOTAL
$10.00
ORDER # 111-1111111-1111111
Your order was cancelled

Order placed
June 2, 2026
Total
$20.00
Order # 222-2222222-2222222

Some Real Product Name That Is Long Enough To Count As An Item Here
"""
    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    assert len(orders) == 1
    assert orders[0].order_id == "222-2222222-2222222"


def test_size_variant_items_kept_as_distinct_not_merged() -> None:
    """Two items differing only by a size/model number (not a badge pair —
    both raw lines are distinct, standalone mentions) are real separate line
    items and must not be collapsed into one, even though they're highly
    similar text.
    """
    parser = AmazonParser()

    order_content = """
    Lee Men's Dungarees New Belted Wyoming Cargo Short, Bourbon, 38
    Return or replace items: Eligible through July 30, 2026
    Buy it again
    View your item

    Lee Men's Dungarees New Belted Wyoming Cargo Short, Bourbon, 36
    Buy it again
    View your item
    """
    items = parser.extract_items_from_content(order_content)
    assert items == [
        "Lee Men's Dungarees New Belted Wyoming Cargo Short, Bourbon, 38",
        "Lee Men's Dungarees New Belted Wyoming Cargo Short, Bourbon, 36",
    ]


def test_quantity_badge_expands_to_one_entry_per_unit() -> None:
    """Trailing quantity badge numbers expand to one item entry per unit bought.

    BEHAVIOR CHANGE: this used to collapse a "Product Name <qty>" / "Product
    Name" badge pair down to a single item entry (see git history for the
    prior "badge stripped" test this replaces). That hid the fact that 2+
    units were purchased and made it impossible to split them into separate
    line items later (e.g. two identical bottles bought in one order). The
    bare name is now repeated once per unit instead, capped at
    MAX_REASONABLE_BADGE_QTY.
    """
    parser = AmazonParser()

    # Amazon shows "Product Name <qty>" then "Product Name" on successive lines.
    order_content = """
    TEMPTATIONS Cat Treats, Seafood Medley Flavour, 454g Tub 4
    TEMPTATIONS Cat Treats, Seafood Medley Flavour, 454g Tub
    Buy it again
    View your item
    Nizoral Anti-Dandruff Shampoo with 2% Ketoconazole, Fresh Scent, 325 ml
    Nizoral Anti-Dandruff Shampoo with 2% Ketoconazole, Fresh Scent, 325 ml
    """

    items = parser.extract_items_from_content(order_content)
    # 4x TEMPTATIONS + 1x Nizoral
    assert len(items) == 5
    assert sum(1 for i in items if "454g Tub" in i) == 4
    assert sum(1 for i in items if "Ketoconazole" in i) == 1
    # Bare form preserved, badge number gone
    assert not any(i.endswith(" 4") for i in items)


def test_quantity_badge_qty_capped_at_reasonable_maximum() -> None:
    """An implausibly large trailing number is treated as part of the title, not a badge."""
    parser = AmazonParser()

    # "500" here is meant to read as a coincidental trailing number (e.g. part of
    # a model/style number), not "500 units purchased" — far outside any
    # realistic bulk-buy quantity.
    order_content = """
    Really Long Product Title With A Trailing Number Like A Model Code 500
    Really Long Product Title With A Trailing Number Like A Model Code
    Buy it again
    """
    items = parser.extract_items_from_content(order_content)
    assert len(items) == 1
    assert not items[0].endswith(" 500")


def test_size_number_not_stripped() -> None:
    """Trailing size numbers (2+ digits) are NOT stripped — they are part of the name."""
    parser = AmazonParser()

    order_content = """
    Corset Femme Tops Floral Renaissance Pirate Overbust Boned Bustier Tops Green Size 10
    Corset Femme Tops Floral Renaissance Pirate Overbust Boned Bustier Tops Green Size 10
    Return items: Eligible through May 17, 2026
    Buy it again
    """

    items = parser.extract_items_from_content(order_content)
    assert len(items) == 1
    assert items[0].endswith("Size 10")


def test_now_arriving_status_line_skipped() -> None:
    """'Now arriving today X:XX p.m.' delivery status must not be extracted as an item."""
    parser = AmazonParser()

    order_content = """
Now arriving today 5:15 p.m. - 8:15 p.m.

    Yogti Psyllium Husk Capsules, 300 Count
    Yogti Psyllium Husk Capsules, 300 Count
    Buy it again
    """

    items = parser.extract_items_from_content(order_content)
    assert all("arriving" not in i.lower() for i in items)
    assert any("Psyllium" in i for i in items)


def test_your_package_delivery_status_skipped() -> None:
    """'Your package was left near the front door or porch.' must not be extracted as an item.

    Regression test: this delivery-status sentence was previously misidentified as a
    product because it (a) didn't start with the literal 'Package was' the old skip
    pattern required, and (b) contains 'pack' as a substring of 'package', which used
    to falsely satisfy the pack-count product heuristic.
    """
    parser = AmazonParser()

    order_content = """
    Your package was left near the front door or porch.

    Muslogy Dashboard Storage Organizer Tray Compatible with Ford Maverick 2024 2022-2023 & Hybrid XL XLT Lariat Accessories Dash Insert Tray Behind Screen Won't Fit 2025 Maverick (Black)
    Muslogy Dashboard Storage Organizer Tray Compatible with Ford Maverick 2024 2022-2023 & Hybrid XL XLT Lariat Accessories Dash Insert Tray Behind Screen Won't Fit 2025 Maverick (Black)
    Buy it again
    """

    items = parser.extract_items_from_content(order_content)
    assert all("left near the front door" not in i.lower() for i in items)
    assert any("Muslogy" in i for i in items)


def test_package_word_does_not_false_match_pack_heuristic() -> None:
    """A line containing 'package'/'packaging' shouldn't count as a pack-size signal."""
    parser = AmazonParser()

    # Short line, contains "package", no other product signals, under 5 words.
    order_content = "Package was damaged"

    items = parser.extract_items_from_content(order_content)
    assert items == []


def test_skip_words_do_not_false_match_inside_real_product_words() -> None:
    """Regression: skip_words used plain substring matching, so a single word
    like 'cart' silently matched inside 'Carton'/'Cartridge', dropping real
    item lines entirely. Real example: a 3M surgical tape order parsed with
    zero items because '12 Rolls/Carton' tripped the 'cart' skip word.
    """
    parser = AmazonParser()

    order_content = """
    3M(TM) Micropore(TM) Surgical Tape 1530-1, 1 IN x 10 YD (2,5cm x 9,1m), 12 Rolls/Carton
    3M(TM) Micropore(TM) Surgical Tape 1530-1, 1 IN x 10 YD (2,5cm x 9,1m), 12 Rolls/Carton
    Return or replace items: Eligible through July 25, 2026
    Buy it again
    """
    items = parser.extract_items_from_content(order_content)
    assert len(items) == 1
    assert "Carton" in items[0]

    # A few more single-word skip_words with the same false-positive shape.
    assert (
        parser.extract_items_from_content(
            "HP 63XL Tri-Color Ink Cartridge for Inkjet Printers Genuine Original"
        )
        != []
    )
    assert (
        parser.extract_items_from_content(
            "A Primer on Machine Learning for Beginners Illustrated Guide Book"
        )
        != []
    )
    assert (
        parser.extract_items_from_content(
            "Digital Voice Recorders for Meetings and Lectures Portable USB Device"
        )
        != []
    )


def test_product_titles_starting_with_ui_action_words_not_skipped() -> None:
    """Regression: the UI-boilerplate skip pattern for words like 'View',
    'Return', 'Get', 'Write', 'Share', 'Leave', and 'Ask' matched as a line
    *prefix* (anchored at line-start with no trailing word boundary), so a
    real product title merely starting with one of those letter sequences
    was silently dropped, e.g. "Viewsonic" (starts with "View"), "Writerly"
    (starts with "Write"), "Getting Things Done" (starts with "Get").
    """
    parser = AmazonParser()

    order_content = """
    Viewsonic 24-inch Full HD IPS Monitor with Built-in Speakers
    Viewsonic 24-inch Full HD IPS Monitor with Built-in Speakers
    Buy it again
    """
    items = parser.extract_items_from_content(order_content)
    assert len(items) == 1
    assert "Viewsonic" in items[0]

    assert (
        parser.extract_items_from_content(
            "Writerly Leather Notebook Cover for A5 Journals and Planners"
        )
        != []
    )
    assert (
        parser.extract_items_from_content(
            "Getting Things Done: The Art of Stress-Free Productivity Book"
        )
        != []
    )


def test_footer_boilerplate_not_extracted() -> None:
    """Page footer and Amazon nav boilerplate must not appear as items."""
    parser = AmazonParser()

    order_content = """
    Tampax Pearl Tampons, Plastic Applicator, Light Absorbency, 50 Count
    Buy it again
    Protect & Build Your Brand
    Amazon.ca Rewards Mastercard
    Registry & Gift List
    Find, attract and engage customers
    Interest-Based Ads
    © 1996-2026, Amazon.com, Inc. or its affiliates
    Amazon.com.ca ULC | 40 King Street W 47th Floor, Toronto, Ontario, Canada, M5H 3Y2
    Influencers & Associates
    To move between items, use your keyboard's up or down arrows.
    """

    items = parser.extract_items_from_content(order_content)

    assert len(items) == 1
    assert "Tampax" in items[0]
    for word in [
        "Mastercard",
        "Registry",
        "attract",
        "Interest-Based",
        "affiliates",
        "ULC",
        "Influencers",
        "keyboard",
    ]:
        assert not any(word.lower() in item.lower() for item in items), (
            f"Boilerplate '{word}' leaked into items"
        )


def test_subscription_order_is_parsed_without_item_bleed() -> None:
    """Digital subscription blocks parse independently between physical orders."""
    order_text = """
    Order placed
    July 7, 2026
    Total
    $45.68
    Ship to
    Shelby and Kalman Sutker
    Order # 701-0590458-8308219
    View order details Invoice

    Delivered 7 July
    Package was left near the front door or porch

    Natural Factors Stress Relax Kava Kava 250 mg, 60 Vegetarian Capsules,
    Promotes Relaxation & A Sense of Calm, 30% Kavalactones, Proudly Canadian
    Natural Factors Stress Relax Kava Kava 250 mg, 60 Vegetarian Capsules,
    Promotes Relaxation & A Sense of Calm, 30% Kavalactones, Proudly Canadian
    Buy it again
    Track package

    Subscription charged on
    July 5, 2026
    Total
    $10.07
    Order # D01-3004731-5334665
    View order details Invoice

    Audible Standard Plus Audiobook Subscription, 1 Month Plan
    Audible Standard Plus Audiobook Subscription, 1 Month Plan
    Audiobook

    Write a product review

    Order placed
    July 5, 2026
    Total
    $10.49
    Ship to
    Shelby and Kalman Sutker
    Order # 702-9401622-4821053
    View order details Invoice

    Yupik Organic Tapioca Starch 1kg, USDA Certified, Gluten-Free, GMO-Free
    Yupik Organic Tapioca Starch 1kg, USDA Certified, Gluten-Free, GMO-Free
    Buy it again
    """

    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    assert len(orders) == 3
    first_order_items = " ".join(orders[0].items)
    assert "Natural Factors" in first_order_items
    assert "Audible Standard Plus" not in first_order_items
    assert orders[1].order_id == "D01-3004731-5334665"
    assert "Audible Standard Plus" in " ".join(orders[1].items)
    assert "Yupik" in " ".join(orders[2].items)


def test_recommendations_after_pagination_do_not_leak_into_last_order() -> None:
    """Product carousels after order pagination are not part of the final order."""
    order_text = """
    Order placed
    July 4, 2026
    Total
    $42.36
    Ship to
    Shelby and Kalman Sutker
    Order # 701-5301082-7301831
    View order details Invoice

    Delivered 6 July
    Package was left near the front door or porch

    KITCHENAID Evergreen Design Series Herringbone Ribbed Soft Silicone Oven Mitts 2-Pack Set, Heat Resistant up to 500F, Deep Forest Green, 7"x14"
    KITCHENAID Evergreen Design Series Herringbone Ribbed Soft Silicone Oven Mitts 2-Pack Set, Heat Resistant up to 500F, Deep Forest Green, 7"x14"
    Buy it again
    View your item

    ←Previous
    1
    2
    3
    Next→

    Learn more
    $180
    You could have saved in the past year with Amazon Business
    Create a free business account
    Buy it again

    Huggies Goodnites Training Pants, Girls Bedwetting NightTime Underwear,
    Size XS, 28-43 lbs, 44 Count, Giga Pack
    Huggies Goodnites Training Pants, Girls Bedwetting NightTime Underwear,
    Size XS, 28-43 lbs, 44 Count, Giga Pack
    $26.98 ($0.61/count)
    Add to Cart

    Top Smart Home Products For You

    Blink Outdoor 4 Wireless smart security camera, two-year battery life,
    1080p HD day and infrared night live view
    Blink Outdoor 4 Wireless smart security camera, two-year battery life,
    1080p HD day and infrared night live view
    $94.99
    Add to Cart
    """

    parser = AmazonParser()
    orders = parser.parse_orders_page(order_text)

    assert len(orders) == 1
    item_text = " ".join(orders[0].items)
    assert "KITCHENAID" in item_text
    assert "Huggies" not in item_text
    assert "Blink Outdoor" not in item_text
    assert "Amazon Business" not in item_text


@pytest.mark.parametrize(
    "status_line",
    [
        "Your delivery is taking longer than expected",
        "Your return is being processed",
    ],
)
def test_your_delivery_status_skipped(status_line: str) -> None:
    """Delivery/return status lines on order pages must not become items."""
    parser = AmazonParser()

    order_content = f"""
    {status_line}
    Reflective Pet Triangle Scarf Safety Bandana for Night Walking, Small
    Buy it again
    """

    items = parser.extract_items_from_content(order_content)

    assert status_line not in items
    assert any("Reflective Pet Triangle Scarf" in i for i in items)
