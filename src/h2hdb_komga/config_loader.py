class KomgaConfig:
    __slots__ = [
        "base_url",
        "api_username",
        "api_password",
        "library_id",
        "trigger_scan",
    ]

    def __init__(
        self,
        base_url: str,
        api_username: str,
        api_password: str,
        library_id: str,
        trigger_scan: bool = True,
    ) -> None:
        self.base_url = base_url
        self.api_username = api_username
        self.api_password = api_password
        self.library_id = library_id
        self.trigger_scan = trigger_scan
