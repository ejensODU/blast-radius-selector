IMAGE ?= rwx-blast-radius
KIT ?= ../starter
KIT_PATH := $(abspath $(KIT))

.PHONY: build sample test

build:
	docker build --platform linux/amd64 -t $(IMAGE) .

sample: build
	docker run --rm --platform linux/amd64 --network none --cpus 2 \
		--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
		--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
		--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
		-v "$(KIT_PATH)/sample/commits:/data/commits:ro" \
		-v "$(KIT_PATH)/sample/tests.json:/data/tests.json:ro" \
		$(IMAGE) > output.jsonl
	python3 "$(KIT_PATH)/score.py" "$(KIT_PATH)/sample" "$(KIT_PATH)/sample/answers.json" output.jsonl

test:
	python3 -m unittest discover -s tests
