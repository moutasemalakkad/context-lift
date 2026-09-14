# Each measure below reproduces one construct from Looker's own public project
# `looker-open-source/thelook`, which is where the defects these tests pin were
# actually found. That project ships no LICENSE file, so it is not vendored
# here; the constructs are reproduced instead and the source is named.
view: order_items {
  sql_table_name: demo.order_items ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: sale_price {
    type: number
    sql: ${TABLE}.sale_price ;;
    description: "What the customer paid for this one item."
  }

  dimension: hidden_internal {
    hidden: yes
    type: string
    sql: ${TABLE}.internal ;;
  }

  # type: sum -- the argument alone is a WRONG whole, not an honest half.
  measure: total_sale_price {
    type: sum
    sql: ${sale_price} ;;
  }

  measure: average_sale_price {
    type: average
    sql: ${sale_price} ;;
  }

  # type: count takes no sql and counts the view's PRIMARY KEY, not rows.
  measure: count {
    type: count
  }

  # The modern filter syntax. Drop the filter and the metric means something
  # else entirely, while reading as though it were complete.
  measure: first_purchase_count {
    type: count
    filters: {
      field: is_first_purchase
      value: "Yes"
    }
  }

  # The legacy filter syntax, which the parser stores in a different shape
  # under the same key.
  measure: completed_count {
    type: count
    filters: [status: "Complete", cancelled_at: "-NULL"]
  }

  measure: distinct_orders {
    type: count_distinct
    sql: ${TABLE}.order_id ;;
  }

  # NOT an aggregate: the sql is the entire expression, so wrapping it would
  # be the same class of error in the other direction.
  measure: margin_rate {
    type: number
    sql: 1.0 * ${total_sale_price} / NULLIF(${count}, 0) ;;
    description: "Margin as a share of revenue."
  }

  # Takes another MEASURE, so the expression is incomplete on its own.
  measure: share_of_revenue {
    type: percent_of_total
    sql: ${total_sale_price} ;;
  }
}
