"""Run many model requests as one Message Batch, at half price, resumably."""

import json
import time

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from common import write_json_safely


def run(client, params_by_id, state_file, poll_seconds=60):
    """Run one request per id and return ({id: message}, failed count, batch id).

    The batch id is saved to state_file as soon as the batch is submitted. If
    that file already exists, the batch it names is waited for and collected
    instead, and params_by_id is ignored: a stopped script picks up where it
    was rather than paying twice. The caller deletes state_file once it has
    safely stored the results.
    """
    if state_file.exists():
        batch_id = json.loads(state_file.read_text())["id"]
        print(f"  batch: resuming {batch_id}")
    else:
        requests = [
            Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(**params))
            for custom_id, params in params_by_id.items()
        ]
        batch_id = client.messages.batches.create(requests=requests).id
        write_json_safely({"id": batch_id, "requests": len(requests)}, state_file)
        print(f"  batch: submitted {batch_id} with {len(requests)} requests")

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  batch {batch.processing_status}: {counts.succeeded} done, "
            f"{counts.processing} processing, {counts.errored} errored"
        )
        if batch.processing_status == "ended":
            break
        time.sleep(poll_seconds)

    messages, failed = {}, 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            messages[result.custom_id] = result.result.message
        else:
            failed += 1
    return messages, failed, batch_id
