import io
import warnings
from multiprocessing.pool import ThreadPool, Pool
from typing import Optional, List, Tuple

from PIL import Image
from jina import Executor, requests, DocumentArray
from jina.logging.logger import JinaLogger

from clip_server.model import clip


class CLIPEncoder(Executor):
    def __init__(
        self,
        name: str = 'ViT-B/32',
        device: Optional[str] = None,
        jit: bool = False,
        num_worker_preprocess: int = 4,
        minibatch_size: int = 64,
        pool_backend: str = 'thread',
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.logger = JinaLogger(self.__class__.__name__)

        import torch

        num_threads = torch.get_num_threads() // self.runtime_args.replicas
        if num_threads < 2:
            self.logger.warning(
                f'Too many clip encoder replicas ({self.runtime_args.replicas})'
                'that would exhaust CPU resources.'
            )

        # NOTE: make sure to set the threads right after the torch import,
        # and `torch.set_num_threads` always take precedence over environment variables `OMP_NUM_THREADS`.
        # For more details, please see https://pytorch.org/docs/stable/generated/torch.set_num_threads.html
        # FIXME: This hack would harm the performance in K8S deployment.
        torch.set_num_threads(max(num_threads, 1))

        if not device:
            self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self._device = device
        self._minibatch_size = minibatch_size
        self._model, self._preprocess_blob, self._preprocess_tensor = clip.load(
            name, device=self._device, jit=jit
        )
        if pool_backend == 'thread':
            self._pool = ThreadPool(processes=num_worker_preprocess)
        else:
            self._pool = Pool(processes=num_worker_preprocess)

    def _preproc_image(self, da: 'DocumentArray') -> 'DocumentArray':
        for d in da:
            if d.tensor is not None:
                d.tensor = self._preprocess_tensor(d.tensor)
            else:
                if not d.blob and d.uri:
                    # in case user uses HTTP protocol and send data via curl not using .blob (base64), but in .uri
                    d.load_uri_to_blob()
                d.tensor = self._preprocess_blob(d.blob)
        da.tensors = da.tensors.to(self._device)
        return da

    def _preproc_text(self, da: 'DocumentArray') -> Tuple['DocumentArray', List[str]]:
        texts = da.texts
        da.tensors = clip.tokenize(texts).to(self._device)
        da[:, 'mime_type'] = 'text'
        return da, texts

    @requests
    async def encode(self, docs: 'DocumentArray', **kwargs):
        _img_da = docs.find(
            {'$or': [{'blob': {'$exists': True}}, {'tensor': {'$exists': True}}]}
        )
        _txt_da = docs.find({'text': {'$exists': True}})

        import torch

        with torch.inference_mode():
            # for image
            if _img_da:
                for minibatch in _img_da.map_batch(
                    self._preproc_image,
                    batch_size=self._minibatch_size,
                    pool=self._pool,
                ):
                    minibatch.embeddings = (
                        self._model.encode_image(minibatch.tensors).cpu().numpy()
                    )

            # for text
            if _txt_da:
                for minibatch, _texts in _txt_da.map_batch(
                    self._preproc_text,
                    batch_size=self._minibatch_size,
                    pool=self._pool,
                ):
                    minibatch.embeddings = (
                        self._model.encode_text(minibatch.tensors).cpu().numpy()
                    )
                    minibatch.texts = _texts

        # drop tensors
        docs.tensors = None
