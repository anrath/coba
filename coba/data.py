"""The data module contains core classes and types for reading and writing data sources.

TODO: Add docstrings for Pipe
TODO: Add unit tests for all Pipe

TODO: Add docstrings for all Sinks
TODO: Add unit tests for all Sinks

TODO: Add docstrings for all Sources
TODO: Add unit tests for all Sources
"""

import collections

from hashlib import md5
from gzip import decompress
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from multiprocessing import Manager, Pool
from threading import Thread
from abc import abstractmethod, ABC
from typing import Any, List, Iterable, Sequence, Dict, Hashable, overload, Optional

from coba.execution import ExecutionContext
from coba.utilities import check_pandas_support
from coba.json import CobaJsonEncoder, CobaJsonDecoder

class Source(ABC):
    @abstractmethod
    def read(self) -> Iterable[Any]:
        ...

class Sink(ABC):

    @abstractmethod
    def write(self, items:Iterable[Any]) -> None:
        ...

class Filter(ABC):
    @abstractmethod
    def filter(self, items:Iterable[Any]) -> Iterable[Any]:
        ...

class StopPipe(Exception):
    pass

class Pipe:

    class SourceFilters(Source):
        def __init__(self, source: Source, filters: Iterable[Filter]) -> None:
            self._source = source
            self._filters = filters
        
        def read(self) -> Iterable[Any]:
            items = self._source.read()

            for filt in self._filters:
                items = filt.filter(items)

            return items

    class FiltersSink(Sink):
        def __init__(self, filters: Iterable[Filter], sink: Sink) -> None:
            self._filters = filters
            self._sink    = sink

        def final_sink(self) -> Sink:
            if isinstance(self._sink, Pipe.FiltersSink):
                return self._sink.final_sink()
            else:
                return self._sink

        def write(self, items: Iterable[Any]):
            for filt in self._filters:
                items = filt.filter(items)
            self._sink.write(items)

    @overload
    @staticmethod
    def join(source: Source, filters: Iterable[Filter]) -> Source:
        ...
    
    @overload
    @staticmethod
    def join(filters: Iterable[Filter], sink: Sink) -> Sink:
        ...

    @overload
    @staticmethod
    def join(source: Source, sink: Sink) -> 'Pipe':
        ...

    @overload
    @staticmethod
    def join(source: Source, filters: Iterable[Filter], sink: Sink) -> 'Pipe':
        ...

    @staticmethod #type: ignore
    def join(*args):
        if len(args) == 3:
            return Pipe(*args)

        if len(args) == 2:
            if isinstance(args[1], collections.Sequence):
                return Pipe.SourceFilters(args[0], args[1])
            elif isinstance(args[0], collections.Sequence):
                return Pipe.FiltersSink(args[0], args[1])
            else:
                return Pipe(args[0], [], args[1])

        raise Exception("An unknown pipe was joined.")

    def __init__(self, source: Source, filters: Sequence[Filter], sink: Sink) -> None:
        self._source  = source
        self._filters = filters
        self._sink    = sink

    def run(self, processes=1, maxtasksperchild=None) -> None:

        if processes == 1:
            try:
                items = self._source.read()

                for filt in self._filters:
                    items = filt.filter(items)

                self._sink.write(items)
            except StopPipe:
                pass
        else:

            if len(self._filters) == 0:
                raise Exception("There was nothing to multi-process within the pipe.")

            with Pool(processes, maxtasksperchild=maxtasksperchild) as pool, Manager() as manager:

                merge_queue = manager.Queue() #type: ignore

                merge_sink   = QueueSink(merge_queue)
                merge_source = QueueSource(merge_queue)

                split_source = map(lambda i: [i], self._source.read())
                split_sink   = Pipe.join(self._filters, merge_sink)
                merge_pipe   = Pipe.join(merge_source, self._sink)

                merge_thread = Thread(target = merge_pipe.run)
                merge_thread.start()
                
                try:
                    for err in pool.imap(ErrorSink(split_sink).write, split_source):
                        if isinstance(err, StopPipe):
                            raise StopPipe()
                except StopPipe:
                    pool.terminate()
                    pool.join()

                merge_queue.put(None)
                merge_thread.join()

class JsonEncode(Filter):
    def filter(self, items: Iterable[Any]) -> Iterable[Any]:
        encoder = CobaJsonEncoder()
        for item in items: yield encoder.encode(item)

class JsonDecode(Filter):
    def filter(self, items: Iterable[Any]) -> Iterable[Any]:
        decoder = CobaJsonDecoder()
        for item in items: yield decoder.decode(item)

class DiskSource(Source):
    def __init__(self, filename:str):
        self.filename = filename

    def read(self) -> Iterable[str]:
        with open(self.filename, "r+") as f:
            for line in f:
                yield line

class DiskSink(Sink):
    def __init__(self, filename:str, mode:str='a+'):
        self.filename = filename
        self._mode    = mode

    def write(self, items: Iterable[str]) -> None:
        with open(self.filename, self._mode) as f:
            for item in items: f.write(item + '\n')

class MemorySource(Source):
    def __init__(self, source: Iterable[Any] = None):
        self._source = source if source is not None else []

    def read(self) -> Iterable[Any]:
        return self._source

class MemorySink(Sink):
    def __init__(self, sink: List[Any] = None):
        self.items = sink if sink is not None else []

    def write(self, items: Iterable[Any]) -> None:
        for item in items:
            self.items.append(item)

class QueueSource(Source):
    def __init__(self, source: Any, poison=None) -> None:
        self._queue  = source
        self._poison = None

    def read(self) -> Iterable[Any]:
        while True:
            item = self._queue.get()

            if item == self._poison:
                return

            yield item

class QueueSink(Sink):
    def __init__(self, sink: Any) -> None:
        self._queue = sink

    def write(self, items:Iterable[Any]) -> None:
        for item in items: self._queue.put(item)

class ErrorSink(Sink):
    def __init__(self, sink:Sink):
        self._sink = sink

    def write(self, items: Iterable[Any]) -> Optional[Exception]:
        try:
            self._sink.write(items)
            return None
        
        except Exception as e:
            return e

        except KeyboardInterrupt:
            # if you are here because keyboard interrupt isn't working for multiprocessing
            # or you want to improve it in some way I can only say good luck. After many hours
            # of spelunking through stackoverflow and the python stdlib I still don't understand
            # why it works the way it does. I arrived at this solution not based on understanding
            # but based on experimentation. This seemed to fix my problem.
            return None

class HttpSource(Source):
    def __init__(self, url: str, file_extension: str = None, checksum: str = None, desc: str = "") -> None:
        self._url       = url
        self._checksum  = checksum
        self._desc      = desc
        self._cachename = f"{md5(self._url.encode('utf-8')).hexdigest()}{file_extension}"

    def read(self) -> Iterable[str]:

        try:

            bites = self._get_bytes()

            if self._checksum is not None and md5(bites).hexdigest() != self._checksum:
                message = (
                    f"The dataset at {self._url} did not match the expected checksum. This could be the result of "
                    "network errors or the file becoming corrupted. Please consider downloading the file again "
                    "and if the error persists you may want to manually download and reference the file.")
                raise Exception(message) from None

            if self._cachename not in ExecutionContext.FileCache: ExecutionContext.FileCache.put(self._cachename, bites)

            return bites.decode('utf-8').splitlines()

        except HTTPError as e:
            if e.code == 412 and 'openml' in self._url:
            
                message = (
                    "An API Key is needed to access openml's rest API. A key can be obtained by creating an "
                    "openml account at openml.org. Once a key has been obtained it should be placed within "
                    "~/.coba as { \"openml_api_key\" : \"<your key here>\", }.")
                raise Exception(message) from None
            
            else:
                raise
    
    def _get_bytes(self) -> bytes:
        if self._cachename in ExecutionContext.FileCache:
            with ExecutionContext.Logger.log(f'loading {self._desc} from cache... '.replace('  ', ' ')):
                return ExecutionContext.FileCache.get(self._cachename)
        else:
            with ExecutionContext.Logger.log(f'loading {self._desc} from http... '):
                with urlopen(Request(self._url, headers={'Accept-encoding':'gzip'})) as response:
                    if response.info().get('Content-Encoding') == "gzip":
                        return decompress(response.read())
                    else:
                        return response.read()

class Table:
    """A container class for storing tabular data."""

    def __init__(self, name:str, primary: Sequence[str], default=float('nan')):
        """Instantiate a Table.
        
        Args:
            name: The name of the table.
            default: The default values to fill in missing values with
        """
        self._name    = name
        self._primary = primary
        self._columns = list(primary)
        self._default = default

        self.rows: Dict[Hashable, Sequence[Any]] = {}

    def add_row(self, *row, **kwrow) -> None:
        """Add a row of data to the table. The row must contain all primary columns."""

        if kwrow:
            self._columns.extend([col for col in kwrow if col not in self._columns])

        row = row + tuple( kwrow.get(col, self._default) for col in self._columns[len(row):] ) #type:ignore
        self.rows[row[0] if len(self._primary) == 1 else tuple(row[0:len(self._primary)])] = row

    def get_row(self, key: Hashable) -> Dict[str,Any]:
        row = self.rows[key]
        row = list(row) + [self._default] * (len(self._columns) - len(row))

        return {k:v for k,v in zip(self._columns,row)}

    def rmv_row(self, key: Hashable) -> None:
        self.rows.pop(key, None)

    def get_where(self, **kwrow) -> Iterable[Dict[str,Any]]:
        idx_val = [ (self._columns.index(col), val) for col,val in kwrow.items() ]

        for key,row in self.rows.items():
            if all( row[i]==v for i,v in idx_val):
                yield {k:v for k,v in zip(self._columns,row)}

    def rmv_where(self, **kwrow) -> None:
        idx_val = [ (self._columns.index(col), val) for col,val in kwrow.items() ]
        rmv_keys  = []

        for key,row in self.rows.items():
            if all( row[i]==v for i,v in idx_val):
                rmv_keys.append(key)

        for key in rmv_keys: 
            del self.rows[key] 

    def to_tuples(self) -> Sequence[Any]:
        """Convert a table into a sequence of namedtuples."""
        return list(self.to_indexed_tuples().values())

    def to_indexed_tuples(self) -> Dict[Hashable, Any]:
        """Convert a table into a mapping of keys to tuples."""

        my_type = collections.namedtuple(self._name, self._columns) #type: ignore #mypy doesn't like dynamic named tuples
        my_type.__new__.__defaults__ = (self._default, ) * len(self._columns) #type: ignore #mypy doesn't like dynamic named tuples
        
        return { key:my_type(*value) for key,value in self.rows.items() } #type: ignore #mypy doesn't like dynamic named tuples

    def to_pandas(self) -> Any:
        """Convert a table into a pandas dataframe."""

        check_pandas_support('Table.to_pandas')
        import pandas as pd #type: ignore #mypy complains otherwise

        return pd.DataFrame(self.to_tuples())

    def __contains__(self, primary) -> bool:

        if isinstance(primary, collections.Mapping):
            primary = list(primary.values())[0] if len(self._primary) == 1 else tuple([primary[col] for col in self._primary])

        return primary in self.rows

    def __str__(self) -> str:
        return str({"Table": self._name, "Columns": self._columns, "Rows": len(self.rows)})

    def __repr__(self) -> str:
        return str(self)

    def __len__(self) -> int:
        return len(self.rows)