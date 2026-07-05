"""Sales report generator with intentional runtime bugs.

Companion program for docs/tutorial-mcp-debugging.md, which walks
through finding the bugs with an AI agent driving tdb over MCP.
The bugs are deliberately NOT marked in this file — reading the code
top to bottom, everything looks plausible. Run it and compare:

    expected grand total:  $44.40
    actual grand total:   $134.70

(If you want to test an agent "honestly", delete this docstring first
so it can't tell the file is a plant.)
"""

BULK_QTY = 10
BULK_DISCOUNT = 0.10  # 10% off orders of BULK_QTY or more units


def line_total(unit_price, qty):
    """Price for one line item, applying the bulk discount when the
    quantity reaches BULK_QTY."""
    total = unit_price * qty
    if qty > BULK_QTY:
        total *= 1 - BULK_DISCOUNT
    return round(total, 2)


def group_by_category(items):
    """Bucket line items by their category."""
    categories = {item["category"] for item in items}
    groups = dict.fromkeys(categories, [])
    for item in items:
        groups[item["category"]].append(item)
    return groups


def build_report(items):
    """Return ({category: (item_count, subtotal)}, grand_total)."""
    groups = group_by_category(items)
    report = {}
    for category, group in sorted(groups.items()):
        subtotal = sum(line_total(i["unit_price"], i["qty"]) for i in group)
        report[category] = (len(group), round(subtotal, 2))
    grand_total = round(sum(sub for _, sub in report.values()), 2)
    return report, grand_total


def main():
    items = [
        {"name": "apples", "category": "fruit", "unit_price": 0.50, "qty": 10},
        {"name": "bananas", "category": "fruit", "unit_price": 0.25, "qty": 12},
        {"name": "cherries", "category": "fruit", "unit_price": 4.00, "qty": 2},
        {"name": "milk", "category": "dairy", "unit_price": 3.50, "qty": 2},
        {"name": "yogurt", "category": "dairy", "unit_price": 1.10, "qty": 6},
        {"name": "bread", "category": "bakery", "unit_price": 2.50, "qty": 3},
        {"name": "bagels", "category": "bakery", "unit_price": 0.75, "qty": 12},
    ]
    report, grand_total = build_report(items)
    for category, (count, subtotal) in report.items():
        print(f"{category:<8} {count} items   subtotal ${subtotal:7.2f}")
    print(f"grand total: ${grand_total:.2f}")


if __name__ == "__main__":
    main()
