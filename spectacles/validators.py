import time
from typing import Any, List, Dict, Sequence, DefaultDict, Union, Optional
from abc import ABC, abstractmethod
from collections import defaultdict
from spectacles.client import LookerClient
from spectacles.lookml import Project, Model, Explore, Dimension
from spectacles.logger import GLOBAL_LOGGER as logger
from spectacles.exceptions import SqlError, DataTestError, SpectaclesException
import spectacles.printer as printer


class Query:
    """Stores IDs and a reference to the LookML object being queried"""

    def __init__(
        self,
        query_id: str,
        lookml_ref: Union[Dimension, Explore],
        query_task_id: Optional[str] = None,
    ):
        self.query_id = query_id
        self.lookml_ref = lookml_ref
        self.query_task_id = query_task_id


class QueryResult:
    """Stores ID, query status, and error details for a completed query task"""

    def __init__(
        self, query_task_id: str, status: str, error: Optional[Dict[str, Any]] = None
    ):
        self.query_task_id = query_task_id
        self.status = status
        self.error = error


class Validator(ABC):  # pragma: no cover
    """Defines abstract base interface for validators.

    Not intended to be used directly, only inherited.

    Attributes:
        client: Looker API client.

    """

    def __init__(self, client: LookerClient):
        self.client = client

    @abstractmethod
    def validate(self):
        raise NotImplementedError


class DataTestValidator(Validator):
    """Runs LookML/data tests for a given project.

    Args:
        client: Looker API client.
        project: Name of the LookML project to validate.

    """

    def __init__(self, client: LookerClient, project: str):
        super().__init__(client)
        self.project = project

    def validate(self) -> List[DataTestError]:
        tests = self.client.all_lookml_tests(self.project)
        test_count = len(tests)
        printer.print_header(
            f"Running {test_count} {'test' if test_count == 1 else 'tests'}"
        )
        errors = []
        test_results = self.client.run_lookml_test(self.project)
        for result in test_results:
            message = f"{result['model_name']}.{result['test_name']}"
            if result["success"]:
                printer.print_validation_result("success", message)
            else:
                for error in result["errors"]:
                    printer.print_validation_result("error", message)
                    errors.append(
                        DataTestError(
                            path=f"{result['model_name']}/{result['test_name']}",
                            message=error["message"],
                        )
                    )
        return errors


class SqlValidator(Validator):
    """Runs and validates the SQL for each selected LookML dimension.

    Args:
        client: Looker API client.
        project: Name of the LookML project to validate.

    Attributes:
        project: LookML project object representation.
        query_tasks: Mapping of query task IDs to LookML objects

    """

    def __init__(self, client: LookerClient, project: str, concurrency: int = 10):
        super().__init__(client)

        self.project = Project(project, models=[])
        self.query_slots = concurrency
        self._running_queries: List[Query] = []
        # Lookup used to retrieve the LookML object
        self._query_by_task_id: Dict[str, Query] = {}

    def get_query_by_task_id(self, query_task_id: str) -> Query:
        return self._query_by_task_id[query_task_id]

    def get_running_query_tasks(self) -> List[str]:
        return [
            query.query_task_id
            for query in self._running_queries
            if query.query_task_id
        ]

    @staticmethod
    def parse_selectors(selectors: List[str]) -> DefaultDict[str, set]:
        """Parses explore selectors with the format 'model_name/explore_name'.

        Args:
            selectors: List of selector strings in 'model_name/explore_name' format.
                The '*' wildcard selects all models or explores. For instance,
                'model_name/*' would select all explores in the 'model_name' model.

        Returns:
            DefaultDict[str, set]: A hierarchy of selected model names (keys) and
                explore names (values).

        """
        selection: DefaultDict = defaultdict(set)
        for selector in selectors:
            try:
                model, explore = selector.split("/")
            except ValueError:
                raise SpectaclesException(
                    f"Explore selector '{selector}' is not valid.\n"
                    "Instead, use the format 'model_name/explore_name'. "
                    f"Use 'model_name/*' to select all explores in a model."
                )
            else:
                selection[model].add(explore)
        return selection

    # TODO: Refactor this so it's more obvious how selection works
    def _select(self, choices: Sequence[str], select_from: Sequence) -> Sequence:
        unique_choices = set(choices)
        select_from_names = set(each.name for each in select_from)
        difference = unique_choices.difference(select_from_names)
        if difference:
            raise SpectaclesException(
                f"{select_from[0].__class__.__name__}"
                f'{"" if len(difference) == 1 else "s"} '
                + ", ".join(difference)
                + f" not found in LookML under project '{self.project.name}'"
            )
        return [each for each in select_from if each.name in unique_choices]

    def build_project(self, selectors: List[str], views: List[str]) -> None:
        """Creates an object representation of the project's LookML.

        Args:
            selectors: List of selector strings in 'model_name/explore_name' format.
                The '*' wildcard selects all models or explores. For instance,
                'model_name/*' would select all explores in the 'model_name' model.

        """
        selection = self.parse_selectors(selectors)
        logger.info(
            f"Building LookML project hierarchy for project {self.project.name}"
        )

        all_models = [
            Model.from_json(model) for model in self.client.get_lookml_models()
        ]
        project_models = [
            model for model in all_models if model.project == self.project.name
        ]

        # Expand wildcard operator to include all specified or discovered models
        selected_model_names = selection.keys()
        if "*" in selected_model_names:
            explore_names = selection.pop("*")
            for model in project_models:
                selection[model.name].update(explore_names)

        selected_models = self._select(
            choices=tuple(selection.keys()), select_from=project_models
        )

        for model in selected_models:
            # Expand wildcard operator to include all specified or discovered explores
            selected_explore_names = selection[model.name]
            if "*" in selected_explore_names:
                selected_explore_names.remove("*")
                selected_explore_names.update(
                    set(explore.name for explore in model.explores)
                )

            selected_explores = self._select(
                choices=tuple(selected_explore_names), select_from=model.explores
            )

            for explore in selected_explores:
                dimensions_json = self.client.get_lookml_dimensions(
                    model.name, explore.name
                )
                for dimension_json in dimensions_json:
                    dimension = Dimension.from_json(dimension_json)
                    dimension.url = self.client.base_url + dimension.url
                    if not dimension.ignore:
                        if len(views) == 0:
                            explore.add_dimension(dimension)
                        elif dimension.view in views:
                            explore.add_dimension(dimension)

            # Remove any explores that have 0 dimensions to test
            selected_explores = [explore for explore in selected_explores if len(explore.dimensions) > 0]

            model.explores = selected_explores

        self.project.models = selected_models

    def validate(self, mode: str = "batch") -> List[SqlError]:
        """Queries selected explores and returns the project tree with errors."""
        explore_count = self._count_explores()
        printer.print_header(
            f"Testing {explore_count} "
            f"{'explore' if explore_count == 1 else 'explores'} "
            f"[{mode} mode]"
        )

        errors = self._create_and_run(mode)
        if mode == "hybrid" and self.project.errored:
            errors = self._create_and_run(mode)

        for model in sorted(self.project.models, key=lambda x: x.name):
            for explore in sorted(model.explores, key=lambda x: x.name):
                message = f"{model.name}.{explore.name}"
                if explore.errored:
                    printer.print_validation_result("error", message)
                else:
                    printer.print_validation_result("success", message)

        return errors

    def _create_and_run(self, mode: str = "batch") -> List[SqlError]:
        """Runs a single validation using a specified mode"""
        queries: List[Query] = []
        try:
            queries = self._create_queries(mode)
            errors = self._run_queries(queries)
        except KeyboardInterrupt:
            logger.info(
                "\n\n" + "Please wait, asking Looker to cancel any running queries"
            )
            query_tasks = self.get_running_query_tasks()
            self._cancel_queries(query_tasks)
            message = "SQL validation was interrupted. "
            if query_tasks:
                message += (
                    f"Attempted to cancel {len(query_tasks)} running "
                    f"{'query' if len(query_tasks) == 1 else 'queries'}."
                )
            else:
                message += "No queries were running at the time."
            raise SpectaclesException(message)
        return errors

    def _create_queries(self, mode: str) -> List[Query]:
        """Creates a list of queries to be executed for validation"""
        queries: List[Query] = []
        for model in self.project.models:
            for explore in model.explores:
                if mode == "batch" or (mode == "hybrid" and not explore.queried):
                    query = self._create_explore_query(explore, model.name)
                    queries.append(query)
                elif mode == "single" or (mode == "hybrid" and explore.errored):
                    explore_queries = self._create_dimension_queries(
                        explore, model.name
                    )
                    queries.extend(explore_queries)
        return queries

    def _create_explore_query(self, explore: Explore, model_name: str) -> Query:
        """Creates a single query with all dimensions of an explore"""
        dimensions = [dimension.name for dimension in explore.dimensions]
        query_id = self.client.create_query(model_name, explore.name, dimensions)
        return Query(query_id, lookml_ref=explore)

    def _create_dimension_queries(
        self, explore: Explore, model_name: str
    ) -> List[Query]:
        """Creates individual queries for each dimension in an explore"""
        queries = []
        for dimension in explore.dimensions:
            query_id = self.client.create_query(
                model_name, explore.name, [dimension.name]
            )
            query = Query(query_id, lookml_ref=dimension)
            queries.append(query)
        return queries

    def _run_queries(self, queries: List[Query]) -> List[SqlError]:
        """Creates and runs queries with a maximum concurrency defined by query slots"""
        QUERY_TASK_LIMIT = 250
        errors: List[SqlError] = []

        while queries or self._running_queries:
            if queries:
                logger.debug(f"Starting a new loop, {len(queries)} queries queued")
                self._fill_query_slots(queries)
            query_tasks = self.get_running_query_tasks()[:QUERY_TASK_LIMIT]
            logger.debug(f"Checking for results of {len(query_tasks)} query tasks")
            for query_result in self._get_query_results(query_tasks):
                sql_error = self._handle_query_result(query_result)
                if sql_error:
                    errors.append(sql_error)
            time.sleep(0.5)
        return errors

    def _fill_query_slots(self, queries: List[Query]) -> None:
        """Creates query tasks until all slots are used or all queries are running"""
        while queries and self.query_slots > 0:
            logger.debug(
                f"{self.query_slots} available query slots, creating query task"
            )
            query = queries.pop(0)
            query_task_id = self.client.create_query_task(query.query_id)
            self.query_slots -= 1
            query.query_task_id = query_task_id
            self._query_by_task_id[query_task_id] = query
            self._running_queries.append(query)

    def _get_completed_query_tasks(
        self, query_task_ids: List[str]
    ) -> List[QueryResult]:
        """Returns ID, status, and error message for completed and errored tasks"""
        query_results = []
        results = self.client.get_query_task_multi_results(query_task_ids)
        for query_task_id, result in results.items():
            status = result["status"]
            logger.debug(f"Query task {query_task_id} status is: {status}")
            if status in ("complete", "error"):
                self.query_slots += 1
                query_result = QueryResult(query_task_id, status)
                if status == "error":
                    try:
                        query_result.error = self._extract_error_details(result)
                    except (KeyError, TypeError, IndexError) as error:
                        raise SpectaclesException(
                            "Encountered an unexpected API query result format, "
                            "unable to extract error details. "
                            f"The query result was: {result}"
                        ) from error
                query_results.append(query_result)
            elif status in ("running", "added", "expired"):
                continue
            else:
                raise SpectaclesException(
                    f'Unexpected query result status "{status}" '
                    "returned by the Looker API"
                )
        return query_results

    def _get_query_results(self, query_task_ids: List[str]) -> List[QueryResult]:
        """Returns ID, status, and error message for all query tasks"""
        query_results = []
        results = self.client.get_query_task_multi_results(query_task_ids)
        for query_task_id, result in results.items():
            status = result["status"]
            if status not in ("complete", "error", "running", "added", "expired"):
                raise SpectaclesException(
                    f'Unexpected query result status "{status}" '
                    "returned by the Looker API"
                )
            logger.debug(f"Query task {query_task_id} status is: {status}")
            query_result = QueryResult(query_task_id, status)
            if status == "error":
                try:
                    error_details = self._extract_error_details(result)
                except (KeyError, TypeError, IndexError) as error:
                    raise SpectaclesException(
                        "Encountered an unexpected API query result format, "
                        "unable to extract error details. "
                        f"The query result was: {result}"
                    ) from error
                else:
                    query_result.error = error_details
            query_results.append(query_result)
        return query_results

    def _handle_query_result(self, result: QueryResult) -> Optional[SqlError]:
        query = self.get_query_by_task_id(result.query_task_id)
        if result.status in ("complete", "error"):
            self._running_queries.remove(query)
            self.query_slots += 1
            lookml_object = query.lookml_ref
            lookml_object.queried = True

            if result.status == "error" and result.error:
                sql_error = SqlError(
                    path=lookml_object.name,
                    url=getattr(lookml_object, "url", None),
                    **result.error,
                )
                lookml_object.error = sql_error
                return sql_error
        return None

    @staticmethod
    def _extract_error_details(query_result: Dict) -> Dict:
        """Extracts the relevant error fields from a Looker API response"""
        data = query_result["data"]
        if isinstance(data, dict):
            errors = data.get("errors") or [data.get("error")]
            first_error = errors[0]
            message = " ".join(
                filter(
                    None,
                    [first_error.get("message"), first_error.get("message_details")],
                )
            )
            sql = data.get("sql")
            error_loc = first_error.get("sql_error_loc")
            if error_loc:
                line_number = error_loc.get("line")
            else:
                line_number = None
        elif isinstance(data, list):
            message = data[0]
            line_number = None
            sql = None
        else:
            raise TypeError(
                "Unexpected error response type. "
                "Expected a dict or a list, "
                f"received type {type(data)}: {data}"
            )

        return {"message": message, "sql": sql, "line_number": line_number}

    def _cancel_queries(self, query_task_ids: List[str]) -> None:
        """Asks the Looker API to cancel specified queries"""
        for query_task_id in query_task_ids:
            self.client.cancel_query_task(query_task_id)

    def _count_explores(self) -> int:
        """Counts the explores in the LookML project hierarchy."""
        explore_count = 0
        for model in self.project.models:
            explore_count += len(model.explores)
        return explore_count
