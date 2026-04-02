class BIDSMetadata(dict):
    """ Modernized BIDSMetadata with better error context. """
    def __init__(self, source_file: str, *args, **kwargs):
        self._source_file = source_file
        super().__init__(*args, **kwargs)

    def __getitem__(self, key):
        if key not in self:
            raise KeyError(
                f"BIDS Metadata Error: '{key}' not found for {self._source_file}. "
                f"Available keys: {list(self.keys())}"
            )
        return super().__getitem__(key)