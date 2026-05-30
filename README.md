# kinoforge

kinoforge is a configuration-driven video-generation orchestrator. It abstracts over GPU compute providers (RunPod, SkyPilot, local), generation engines (ComfyUI, Diffusers, hosted APIs), and model sources (HuggingFace, CivitAI, plain HTTPS) behind a single YAML config file and a small CLI. Swapping providers, engines, or model sources requires only a config edit — no code changes, no branching on provider names in core logic.

## Quickstart

```bash
# Install dependencies
pixi install

# Dry-run: print the deployment plan without touching any cloud resources
pixi run python -m kinoforge --state-dir ~/.kinoforge \
  deploy --config examples/configs/local-fake.yaml --dry-run

# Generate a clip offline (FakeEngine + LocalProvider, no GPU required)
pixi run python -m kinoforge --state-dir ~/.kinoforge \
  generate --config examples/configs/local-fake.yaml \
           --prompt "ocean waves at sunset" --mode t2v --run-id run01
```

Expected output sketch for the dry-run:

```
[dry-run] engine=fake  provider=local
  capability_key: <sha256-prefix>
  offers available: 2
  lifecycle: idle_timeout=3600s  max_lifetime=10800s  budget=$10.00
  models: 1 entry (1 base, 0 lora, 0 vae)
```

## Configuration

Each kinoforge run is described by a single YAML file with three top-level blocks:

```yaml
engine:      # which generation backend to use + precision
models:      # ordered list of model refs (base + optional loras/vae)
compute:     # where to run (provider + image + hardware + lifecycle/budget)
```

For hosted engines (e.g. fal.ai) the `compute:` block is omitted and a top-level `lifecycle: {budget: N}` carries the spend guard instead.

Browse ready-to-use examples in [`examples/configs/`](examples/configs/):

| File | Engine | Provider | Use case |
|------|--------|----------|----------|
| [`wan.yaml`](examples/configs/wan.yaml) | ComfyUI | RunPod pod | Production Wan2.2 + CivitAI LoRA |
| [`diffusers.yaml`](examples/configs/diffusers.yaml) | Diffusers | RunPod serverless | SVD serverless |
| [`hosted.yaml`](examples/configs/hosted.yaml) | Hosted API | fal.ai | Zero-infra hosted |
| [`local-fake.yaml`](examples/configs/local-fake.yaml) | Fake | Local | Offline / CI smoke test |

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

## Roadmap (deferred layers and their seams)

Each item below names the deferred layer and the exact seam it plugs into when built:

- **Continuity / stitching fallback** — `strategy.decide` non-native branch; the fallback path currently issues N single-segment jobs; stitching post-processing slots in between `pool.map` and `store.put_bytes` in `GenerateClipStage`.
- **Audio sync layer** — `strategy.decide` sets `spec["_audio_mode"] = "separate"` as a marker; a downstream audio-sync stage reads this key and schedules audio generation after the video clip is stored.
- **Concurrent / distributed backend scheduler** — `BackendPool` ABC (alongside `SequentialPool`); drop in a `ThreadedPool` or `RayPool` implementation and inject it into `GenerateClipStage`.
- **Keyframe / image-generation upstream Stage** — `Stage` Protocol + `ConditioningAsset` with `kind="image"`; add an `ImageGenStage` that satisfies `Stage` and feeds its output into the video generation stage's `segments_override`.
- **Cross-process discovery lock** — `ModelProfileProvider` currently uses an in-process threading.Event for single-flight; replace with a file-lock or Redis-backed lock for multi-process / distributed workers.

## Design references

The `providers/skypilot/` adapter wraps [SkyPilot](https://github.com/skypilot-org/skypilot) (Apache 2.0, UC Berkeley Sky Computing Lab). SkyPilot was a major influence on kinoforge's `ComputeProvider` abstraction, particularly the autostop mapping (`idle_timeout_s → autostop minutes`), the cost-aware GPU offer selection model, and the principle that cloud portability should be configuration-level rather than code-level. We credit the SkyPilot authors and recommend their work for anyone building on cloud-portable ML infrastructure.
