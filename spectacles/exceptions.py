class SpectaclesException(Exception):
    exit_code = 100


class ApiConnectionError(SpectaclesException):
    exit_code = 101


class ValidationError(SpectaclesException):
    exit_code = 102


class SqlError(ValidationError):
    def __init__(
        self,
        path: str,
        message: str,
        sql: str,
        line_number: int = None,
        url: str = None,
    ):
        super().__init__(message)
        self.path = path
        self.message = message
        self.sql = sql
        self.line_number = line_number
        self.url = url

    def __repr__(self):
        if self.line_number is not None:
            sql_line = self.sql.split('\n')[self.line_number - 1]
            return f"{self.message}\n{sql_line}"
        else:
            return self.message


class DataTestError(ValidationError):
    def __init__(self, path: str, message: str):
        super().__init__(message)
        self.path = path
        self.message = message

    def __repr__(self):
        return self.message
