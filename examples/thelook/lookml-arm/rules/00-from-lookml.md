# Metric definitions
A metric marked `counts only rows where` is RESTRICTED: it ignores every other row. The condition is quoted in the source tool's filter language, which is not SQL.
- count (on inventory_items)
    computed as: COUNT(id)
- count (on order_items)
    computed as: COUNT(id)
- total_sale_price (on order_items)
    computed as: SUM(sale_price)
- average_sale_price (on order_items)
    computed as: AVG(sale_price)
- count (on orders)
    computed as: COUNT(id)
- first_purchase_count (on orders)
    computed as: COUNT(id)
    counts only rows where is_first_purchase is "Yes"
- average_order_profit (on orders)
    computed as: AVG(order_profit)
- count (on products)
    computed as: COUNT(id)
- count (on users)
    computed as: COUNT(id)

# How tables join
- inventory_items to products on inventory_items.product_id = products.id (many_to_one)
- order_items to inventory_items on order_items.inventory_item_id = inventory_items.id (many_to_one)
- order_items to orders on order_items.order_id = orders.id (many_to_one)
- order_items to products on inventory_items.product_id = products.id (many_to_one)
- order_items to users on orders.user_id = users.id (many_to_one)
- orders to users on orders.user_id = users.id (many_to_one)