"""Execution of interpreter on data rows.
"""
import collections
import datetime
import itertools

from beancount.core import data
from beancount.query import query_compile
from beancount.parser import options
from beancount.parser import printer
from beancount.ops import summarize
from beancount.utils import misc_utils
from beancount.utils.misc_utils import box


def filter_entries(c_from, entries, options_map):
    """Filter the entries by the given compiled FROM clause.

    Args:
      c_from: A compiled From clause instance.
      entries: A list of directives.
      options_map: A parser's option_map.
    Returns:
      A list of filtered entries.
    """
    assert c_from is None or isinstance(c_from, query_compile.EvalFrom)
    assert isinstance(entries, list)

    if c_from is None:
        return entries

    # Filter the entries with the FROM clause's expression.
    c_expr = c_from.c_expr
    if c_expr is not None:
        entries = [entry for entry in entries if c_expr(entry)]

    account_types = options.get_account_types(options_map)

    # Process the OPEN clause.
    if c_from.open is not None:
        assert isinstance(c_from.open, datetime.date)
        open_date = c_from.open
        entries, index = summarize.open_opt(entries, open_date, options_map)

    # Process the CLOSE clause.
    if c_from.close is not None:
        if isinstance(c_from.close, datetime.date):
            close_date = c_from.close
            entries, index = summarize.close_opt(entries, close_date, options_map)
        elif c_from.close is True:
            entries, index = summarize.close_opt(entries, None, options_map)

    # Process the CLEAR clause.
    if c_from.clear is not None:
        entries, index = summarize.clear_opt(entries, None, options_map)

    return entries


def execute_print(print_stmt, entries, options_map, file):
    """Print entries from a print statement specification.

    Args:
      query: An instance of a compiled Print statemnet.
      entries: A list of directives.
      options_map: A parser's option_map.
      file: The output file to print to.
    """
    if print_stmt and print_stmt.from_clause is not None:
        entries = filter_entries(print_stmt.from_clause, entries, options_map)

    printer.print_entries(entries, file=file)


class Allocator:
    """A helper class to count slot allocations and return unique handles to them.
    """
    def __init__(self):
        self.size = 0

    def allocate(self):
        """Allocate a new slot to store row aggregation information.

        Returns:
          A unique handle used to index into an row-aggregation store (an integer).
        """
        handle = self.size
        self.size += 1
        return handle

    def create_store(self):
        """Create a new row-aggregation store suitable to contain all the node allocations.

        Returns:
          A store that can accomodate and be indexed by all the allocated slot handles.
        """
        return [None] * self.size


def execute_query(query, entries, options_map):
    """Given a compiled select statement, execute the query.

    Args:
      query: An instance of a query_compile.Query
      entries: A list of directives.
      options_map: A parser's option_map.
    Returns:
      A pair of:
        result_types: A list of (name, data-type) item pairs.
        result_rows: A list of ResultRow tuples of length and types described by
          'result_types'.
    """
    # Filter the entries using the WHERE clause.
    if query.c_from is not None:
        entries = filter_entries(query.c_from, entries, options_map)

    # Figure out the result types that describe what we return.
    result_types = [(target.name, target.c_expr.dtype)
                    for target in query.c_targets
                    if target.name is not None]

    # Create a class for each final result.
    ResultRow = collections.namedtuple('ResultRow',
                                       [target.name
                                        for target in query.c_targets
                                        if target.name is not None])

    # Pre-compute lists of the expressions to evaluate.
    group_indexes = (set(query.group_indexes)
                     if query.group_indexes is not None
                     else query.group_indexes)

    # Indexes of the columns for result rows and order rows.
    result_indexes = [index
                      for index, c_target in enumerate(query.c_targets)
                      if c_target.name]
    order_indexes = query.order_indexes

    # Dispatch between the non-aggregated queries and aggregated queries.
    c_where = query.c_where
    schwartz_rows = []
    if query.group_indexes is None:
        # This is a non-aggregated query.

        # Precompute a list of expressions to be evaluated, and of indexes
        # within it for the result rows and the order keys.
        c_target_exprs = [c_target.c_expr
                          for c_target in query.c_targets]

        # Iterate over all the postings once and produce schwartzian rows.
        for entry in entries:
            if isinstance(entry, data.Transaction):
                for posting in entry.postings:
                    if c_where is None or c_where(posting):
                        # Evaluate all the values.
                        values = [c_expr(posting) for c_expr in c_target_exprs]

                        # Compute result and sort-key objects.
                        result = ResultRow._make(values[index]
                                                 for index in result_indexes)
                        sortkey = (tuple(values[index] for index in order_indexes)
                                   if order_indexes is not None
                                   else None)
                        schwartz_rows.append((sortkey, result))
    else:
        # This is an aggregated query.

        # Precompute lists of non-aggregate and aggregate expressions to
        # evaluate. For aggregate targets, we hunt down the aggregate
        # sub-expressions to evaluate, to avoid recursion during iteration.
        c_nonaggregate_exprs = []
        c_aggregate_exprs = []
        for index, c_target in enumerate(query.c_targets):
            c_expr = c_target.c_expr
            if index in group_indexes:
                c_nonaggregate_exprs.append(c_expr)
            else:
                _, aggregate_exprs = query_compile.get_columns_and_aggregates(c_expr)
                c_aggregate_exprs.extend(aggregate_exprs)
        # Note: it is possible that there are no aggregates to compute here. You could
        # have all columns be non-aggregates and group-by the entire list of columns.

        # Pre-allocate handles in aggregation nodes.
        allocator = Allocator()
        for c_expr in c_aggregate_exprs:
            c_expr.allocate(allocator)

        # Iterate over all the postings to evaluate the aggregates.
        agg_store = {}
        for entry in entries:
            if isinstance(entry, data.Transaction):
                for posting in entry.postings:
                    if c_where is None or c_where(posting):
                        # Compute the non-aggregate expressions.
                        row_key = tuple(c_expr(posting)
                                        for c_expr in c_nonaggregate_exprs)

                        # Get an appropriate store for the unique key of this row.
                        try:
                            store = agg_store[row_key]
                        except KeyError:
                            # This is a row; create a new store.
                            store = allocator.create_store()
                            for c_expr in c_aggregate_exprs:
                                c_expr.initialize(store)
                            agg_store[row_key] = store

                        # Update the aggregate expressions.
                        for c_expr in c_aggregate_exprs:
                            c_expr.update(store, posting)

        # Iterate over all the aggregations to produce the schwartzian rows.
        for key, store in agg_store.items():
            key_iter = iter(key)
            values = []
            for index, c_target in enumerate(query.c_targets):
                if index in group_indexes:
                    value = next(key_iter)
                else:
                    value = c_target.c_expr.finalize(store)
                values.append(value)

            # Compute result and sort-key objects.
            result = ResultRow._make(values[index]
                                     for index in result_indexes)
            sortkey = (tuple(values[index] for index in order_indexes)
                       if order_indexes is not None
                       else None)
            schwartz_rows.append((sortkey, result))

    # Order results if requested.
    if order_indexes is not None:
        schwartz_rows.sort(key=lambda x: x[0],
                           reverse=(query.ordering == 'DESC'))

    # Extract final results, in sorted order at this point.
    result_rows = [x[1] for x in schwartz_rows]

    # Apply distinct.
    if query.distinct:
        result_rows = misc_utils.uniquify(result_rows)

    # Apply limit.
    if query.limit is not None:
        result_rows = result_rows[:query.limit]

    return (result_types, result_rows)
