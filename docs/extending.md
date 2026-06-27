# Extending kinoforge

(Moved from README §Extending: add a provider/source/engine (ComputeProvider, ModelSource, GenerationEngine, Splitter, ArtifactStore) on 2026-06-27. See [../README.md](../README.md).)

## Extending: add a provider/source/engine

kinoforge's registry lets you add a new adapter in a single file without touching core. Each pattern follows the same three steps: subclass the ABC, implement the required methods, and call the register function once at module import.

### New ComputeProvider

```python
# src/kinoforge/providers/myprovider/__init__.py
from kinoforge.core.interfaces import (
    ComputeProvider, GpuOffer, InstanceSpec, Instance, Lifecycle,
)
from kinoforge.core.registry import register_provider

class MyProvider(ComputeProvider):
    def find_offers(self, requirements, lifecycle) -> list[GpuOffer]: ...
    def create_instance(self, spec: InstanceSpec) -> Instance: ...
    def get_instance(self, instance_id: str) -> Instance: ...
    def list_instances(self) -> list[Instance]: ...
    def stop_instance(self, instance_id: str) -> None: ...
    def destroy_instance(self, instance_id: str) -> None: ...
    def heartbeat(self, instance_id: str) -> None: ...
    def endpoints(self, instance: Instance) -> dict[str, str]: ...

register_provider("myprovider", MyProvider)
```

Set `compute.provider: myprovider` in your YAML — no other changes.

### New ModelSource

```python
# src/kinoforge/sources/mystore/__init__.py
from kinoforge.core.interfaces import ModelSource, Artifact
from kinoforge.core.registry import register_source

class MyStoreSource(ModelSource):
    def handles(self, ref: str) -> bool:
        return ref.startswith("mystore:")

    def resolve(self, ref: str) -> Artifact:
        # return an Artifact with url + headers
        ...

register_source(MyStoreSource())
```

Use `ref: "mystore:org/model:file.safetensors"` in the `models:` list.

### New GenerationEngine

```python
# src/kinoforge/engines/myengine/__init__.py
from kinoforge.core.interfaces import GenerationEngine, GenerationBackend
from kinoforge.core.registry import register_engine

class MyEngine(GenerationEngine):
    requires_compute: bool = True
    requires_local_weights: bool = True

    def provision(self, instance, cfg) -> None: ...
    def backend(self, instance, cfg) -> GenerationBackend: ...
    def validate_spec(self, spec: dict) -> None: ...

register_engine("myengine", MyEngine)
```

Set `engine.kind: myengine` in your YAML.

### New Splitter

```python
# src/kinoforge/splitters/mysplitter/__init__.py
from kinoforge.core.interfaces import ModelProfile, Segment, Splitter
from kinoforge.core.registry import register_splitter

class MySplitter(Splitter):
    name = "mysplitter"

    def split(
        self, prompt: str, profile: ModelProfile, params: dict
    ) -> list[Segment]:
        # Return ordered segments derived from prompt + profile + params.
        ...

register_splitter("mysplitter", lambda: MySplitter())
```

Set `splitter.kind: mysplitter` in your YAML. The default `"heuristic"` splitter (`core/splitter.py`) splits on blank lines; plug an LLM-semantic or scene-detect strategy here.

### New ArtifactStore

Three stores ship in-tree: `LocalArtifactStore` (filesystem, default), `S3ArtifactStore` (`s3://` URIs, registered as `"s3"`), and `GCSArtifactStore` (`gs://` URIs, registered as `"gcs"`). Add a fourth backend by subclassing the ABC and self-registering:

```python
# src/kinoforge/stores/mystore/__init__.py
from kinoforge.core.interfaces import Artifact
from kinoforge.core.registry import register_store
from kinoforge.stores.base import ArtifactStore

class MyArtifactStore(ArtifactStore):
    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact: ...
    def get_bytes(self, uri: str) -> bytes: ...
    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact: ...
    def get_json(self, uri: str) -> dict: ...
    def list(self, run_id: str) -> list[str]: ...
    def delete(self, uri: str) -> None: ...
    def uri_for(self, run_id: str, name: str) -> str: ...

register_store("mystore", lambda: MyArtifactStore(...))
```

Set `store.kind: mystore` in your YAML.
