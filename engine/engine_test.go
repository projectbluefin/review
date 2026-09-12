package engine

import (
	"context"
	"errors"
	"testing"
	"time"
)

type outputExecutor struct{ payload []byte }

func (x outputExecutor) Run(_ context.Context, emit func(string, []byte)) error {
	emit("stdout", x.payload)
	return nil
}

func fixture(executor string) Spec {
	return Spec{WorkloadKind: "fixture", ExecutorID: executor, Repository: "projectbluefin/review", SourceRevision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", Image: "ghcr.io/projectbluefin/review@sha256:0111111111111111111111111111111111111111111111111111111111111111", ClientProtocol: Version{1, 0}, Limits: Limits{Runtime: time.Second, OutputBytes: 1024}}
}
func wait(t *testing.T, e *Engine, id string) Snapshot {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := e.Wait(ctx, id); err != nil {
		t.Fatal(err)
	}
	got, err := e.Get(id)
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func TestNoopLifecycleAndReceipt(t *testing.T) {
	e := New()
	id, existing, err := e.Start("request-1", fixture("fixture/noop"))
	if err != nil || existing {
		t.Fatalf("start: existing=%v err=%v", existing, err)
	}
	got := wait(t, e, id)
	if got.State != Succeeded || got.Receipt == nil || got.Receipt.Cleanup != "COMPLETE" {
		t.Fatalf("unexpected snapshot: %#v", got)
	}
	events, _ := e.Watch(id, 0)
	for i, event := range events {
		if event.Sequence != uint64(i+1) {
			t.Fatalf("sequence %d at %d", event.Sequence, i)
		}
	}
	if events[len(events)-1].Kind != "terminal_receipt" {
		t.Fatalf("last event: %#v", events[len(events)-1])
	}
	tail, err := e.Watch(id, events[len(events)-2].Sequence)
	if err != nil || len(tail) != 1 || tail[0].Kind != "terminal_receipt" {
		t.Fatalf("reconnect tail=%#v err=%v", tail, err)
	}
}
func TestCancellationIsIdempotent(t *testing.T) {
	e := New()
	id, _, err := e.Start("request-2", fixture("fixture/blocking"))
	if err != nil {
		t.Fatal(err)
	}
	for {
		got, _ := e.Get(id)
		if got.State == Running {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if _, err = e.Cancel(id); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Cancel(id); err != nil {
		t.Fatal(err)
	}
	got := wait(t, e, id)
	if got.State != Cancelled || got.Receipt.Cleanup != "COMPLETE" {
		t.Fatalf("unexpected snapshot: %#v", got)
	}
}
func TestStartIdempotencyAndConflict(t *testing.T) {
	e := New()
	spec := fixture("fixture/noop")
	id, _, _ := e.Start("same", spec)
	id2, existing, err := e.Start("same", spec)
	if err != nil || !existing || id2 != id {
		t.Fatalf("repeat: %q %v %v", id2, existing, err)
	}
	spec.Model = "different"
	if _, _, err = e.Start("same", spec); !errors.Is(err, ErrRequestConflict) {
		t.Fatalf("want conflict, got %v", err)
	}
}
func TestProtocolAndExecutorFailBeforeAllocation(t *testing.T) {
	e := New()
	spec := fixture("fixture/noop")
	spec.ClientProtocol.Major = 2
	if _, _, err := e.Start("bad-version", spec); !errors.Is(err, ErrIncompatibleProtocol) {
		t.Fatal(err)
	}
	spec.ClientProtocol.Major = 1
	spec.ExecutorID = "shell"
	if _, _, err := e.Start("bad-executor", spec); !errors.Is(err, ErrUnsupportedExecutor) {
		t.Fatal(err)
	}
	if len(e.jobs) != 0 {
		t.Fatalf("allocated %d jobs", len(e.jobs))
	}
}
func TestDeadline(t *testing.T) {
	e := New()
	spec := fixture("fixture/blocking")
	spec.Limits.Runtime = 10 * time.Millisecond
	id, _, err := e.Start("deadline", spec)
	if err != nil {
		t.Fatal(err)
	}
	if got := wait(t, e, id); got.State != TimedOut {
		t.Fatalf("state %s", got.State)
	}
}

func TestIdentityAndArtifactBoundary(t *testing.T) {
	e := New()
	spec := fixture("fixture/noop")
	spec.SourceRevision = "main"
	if _, _, err := e.Start("mutable-source", spec); err == nil {
		t.Fatal("mutable source was accepted")
	}
	spec = fixture("fixture/noop")
	spec.Image = "ghcr.io/projectbluefin/review:latest"
	if _, _, err := e.Start("mutable-image", spec); err == nil {
		t.Fatal("mutable image was accepted")
	}
	id, _, err := e.Start("artifacts", fixture("fixture/noop"))
	if err != nil {
		t.Fatal(err)
	}
	if artifacts, err := e.ListArtifacts(id); err != nil || len(artifacts) != 0 {
		t.Fatalf("artifacts=%v err=%v", artifacts, err)
	}
	if _, _, err := e.ReadArtifact(id, "undeclared", 0, 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("read undeclared artifact: %v", err)
	}
}

func TestOutputIsBoundedAndReceiptRecordsTruncation(t *testing.T) {
	e := New()
	e.executors["fixture/output"] = outputExecutor{payload: []byte("too much output")}
	spec := fixture("fixture/output")
	spec.Limits.OutputBytes = 4
	id, _, err := e.Start("bounded-output", spec)
	if err != nil {
		t.Fatal(err)
	}
	got := wait(t, e, id)
	if !got.Receipt.OutputTruncated {
		t.Fatal("receipt did not record truncation")
	}
	events, _ := e.Watch(id, 0)
	found := false
	for _, event := range events {
		if event.Kind == "output_truncated" {
			found = true
		}
		if len(event.Payload) > 4 {
			t.Fatalf("unbounded payload: %q", event.Payload)
		}
	}
	if !found {
		t.Fatal("missing typed truncation event")
	}
}
