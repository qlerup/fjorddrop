package main

import (
	"encoding/binary"
	"net"
	"testing"
)

func TestPrivatePeerDenied(t *testing.T) {
	for _, s := range []string{"127.0.0.1", "10.2.0.1", "192.168.1.250", "172.17.0.1", "169.254.169.254", "100.64.0.1", "0.1.2.3", "224.0.0.1", "::1", "fc00::1"} {
		if allowedPeer(nil, net.ParseIP(s)) {
			t.Fatal("allowed private peer", s)
		}
	}
	if !allowedPeer(nil, net.ParseIP("185.111.109.2")) {
		t.Fatal("public peer rejected")
	}
}
func TestMappingAdvertisesExternalPortAndRejectsChangedLease(t *testing.T) {
	server, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	go func() {
		for {
			b := make([]byte, 32)
			_, addr, e := server.ReadFrom(b)
			if e != nil {
				return
			}
			out := make([]byte, 16)
			out[1] = b[1] + 128
			copy(out[8:10], b[4:6])
			binary.BigEndian.PutUint16(out[10:], 43210)
			copy(out[12:], b[8:12])
			server.WriteTo(out, addr)
		}
	}()
	m := mapper{gateway: server.LocalAddr().String()}
	p, err := m.request(1, 61000, 0, 60)
	if err != nil || p != 43210 {
		t.Fatalf("allocation %d %v", p, err)
	}
	if _, err = m.request(1, 61000, 43211, 60); err == nil {
		t.Fatal("accepted changed mapping")
	}
	if _, err = m.request(1, 61000, 43210, 60); err != nil {
		t.Fatal(err)
	}
}
