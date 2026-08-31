class LiveMicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.frameSamples = options.processorOptions.frameSamples;
    this.frame = new Float32Array(this.frameSamples);
    this.offset = 0;
  }

  process(inputs, outputs) {
    const input = inputs[0][0];
    if (input) {
      let inputOffset = 0;
      while (inputOffset < input.length) {
        const count = Math.min(this.frameSamples - this.offset, input.length - inputOffset);
        this.frame.set(input.subarray(inputOffset, inputOffset + count), this.offset);
        this.offset += count;
        inputOffset += count;
        if (this.offset === this.frameSamples) {
          this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
          this.frame = new Float32Array(this.frameSamples);
          this.offset = 0;
        }
      }
    }
    // Keep the worklet alive without feeding the microphone back to the speakers.
    for (const output of outputs[0]) output.fill(0);
    return true;
  }
}

registerProcessor('live-mic-processor', LiveMicProcessor);
