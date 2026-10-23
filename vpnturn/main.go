// FjordDrop's VPN relay allocates an independent Proton NAT-PMP lease for
// every TURN allocation. Unlike a fixed Coturn range, public ports may differ
// from the bound private ports. File contents remain encrypted by WebRTC.
package main

import (
	"crypto/hmac"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/pion/turn/v4"
)

type mapper struct{ gateway string }

func (m mapper) request(op byte, private, public, lifetime int) (int, error) {
	c, err := net.DialTimeout("udp4", m.gateway, 2*time.Second)
	if err != nil {
		return 0, err
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(2 * time.Second))
	b := make([]byte, 12)
	b[1] = op
	binary.BigEndian.PutUint16(b[4:], uint16(private))
	binary.BigEndian.PutUint16(b[6:], uint16(public))
	binary.BigEndian.PutUint32(b[8:], uint32(lifetime))
	if _, err = c.Write(b); err != nil {
		return 0, err
	}
	b = make([]byte, 32)
	n, err := c.Read(b)
	if err != nil {
		return 0, err
	}
	if n != 16 || b[0] != 0 || b[1] != op+128 || binary.BigEndian.Uint16(b[2:]) != 0 || int(binary.BigEndian.Uint16(b[8:])) != private || int(binary.BigEndian.Uint32(b[12:])) != lifetime {
		return 0, errors.New("NAT-PMP rejected lease")
	}
	assigned := int(binary.BigEndian.Uint16(b[10:]))
	if lifetime > 0 && (assigned < 1024 || (public != 0 && public != assigned)) {
		return 0, errors.New("NAT-PMP port changed")
	}
	return assigned, nil
}

type leaseConn struct {
	net.PacketConn
	m               mapper
	g               *generator
	private, public int
	done            chan struct{}
	once            sync.Once
}

func (c *leaseConn) Close() error {
	var err error
	c.once.Do(func() {
		close(c.done)
		c.g.mu.Lock()
		delete(c.g.ports, c.public)
		delete(c.g.reverse, c.private)
		err = c.PacketConn.Close()
		c.m.request(1, c.private, c.public, 0)
		c.g.mu.Unlock()
	})
	return err
}

// Proton does not hairpin two mappings on the same tunnel. Route those packets
// through the kernel's local sockets and preserve their advertised TURN source.
// Both external clients still connect through the VPN's public listener.
func (c *leaseConn) WriteTo(b []byte, addr net.Addr) (int, error) {
	if udp, ok := addr.(*net.UDPAddr); ok && udp.IP.Equal(c.g.ip) {
		c.g.mu.Lock()
		private, found := c.g.ports[udp.Port]
		c.g.mu.Unlock()
		if found {
			addr = &net.UDPAddr{IP: net.ParseIP(c.g.bind), Port: private}
		}
	}
	return c.PacketConn.WriteTo(b, addr)
}
func (c *leaseConn) ReadFrom(b []byte) (int, net.Addr, error) {
	n, addr, err := c.PacketConn.ReadFrom(b)
	if udp, ok := addr.(*net.UDPAddr); ok && udp.IP.Equal(net.ParseIP(c.g.bind)) {
		c.g.mu.Lock()
		public, found := c.g.reverse[udp.Port]
		c.g.mu.Unlock()
		if found {
			addr = &net.UDPAddr{IP: c.g.ip, Port: public}
		}
	}
	return n, addr, err
}
func (c *leaseConn) renew() {
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-c.done:
			return
		case <-ticker.C:
			if _, err := c.m.request(1, c.private, c.public, 60); err != nil {
				c.Close()
				return
			}
		}
	}
}

type generator struct {
	m              mapper
	ip             net.IP
	bind           string
	first, last    int
	mu             sync.Mutex
	ports, reverse map[int]int
}

func (g *generator) Validate() error {
	if g.ip == nil {
		return errors.New("invalid public IP")
	}
	return nil
}
func (g *generator) AllocateConn(string, int) (net.Conn, net.Addr, error) {
	return nil, nil, errors.New("TCP relay allocations disabled")
}
func (g *generator) AllocatePacketConn(network string, requested int) (net.PacketConn, net.Addr, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if network != "udp4" && network != "udp" {
		return nil, nil, errors.New("IPv4 required")
	}
	if requested != 0 {
		return nil, nil, errors.New("reserved ports unsupported")
	}
	for p := g.first; p <= g.last; p++ {
		conn, err := net.ListenPacket("udp4", net.JoinHostPort(g.bind, strconv.Itoa(p)))
		if err != nil {
			continue
		}
		external, err := g.m.request(1, p, 0, 60)
		if err != nil {
			conn.Close()
			return nil, nil, err
		}
		if g.ports == nil {
			g.ports = map[int]int{}
			g.reverse = map[int]int{}
		}
		if _, used := g.ports[external]; used {
			conn.Close()
			return nil, nil, errors.New("conflicting public relay port")
		}
		g.ports[external] = p
		g.reverse[p] = external
		c := &leaseConn{PacketConn: conn, m: g.m, g: g, private: p, public: external, done: make(chan struct{})}
		go c.renew()
		return c, &net.UDPAddr{IP: g.ip, Port: external}, nil
	}
	return nil, nil, errors.New("all VPN relay slots are in use")
}
func allowedPeer(_ net.Addr, ip net.IP) bool {
	_, shared, _ := net.ParseCIDR("100.64.0.0/10")
	return ip.To4() != nil && ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback() && !ip.IsLinkLocalUnicast() && !shared.Contains(ip) && ip.To4()[0] != 0
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "FjordDrop VPN relay:", err)
		os.Exit(1)
	}
}
func run() error {
	secret, err := os.ReadFile("/state/secret")
	if err != nil {
		return err
	}
	if len(secret) < 32 {
		return errors.New("missing relay secret")
	}
	ip := net.ParseIP(os.Getenv("PUBLIC_IP"))
	if ip == nil {
		return errors.New("invalid VPN IP")
	}
	port, err := strconv.Atoi(os.Getenv("LISTEN_PORT"))
	if err != nil || port < 1024 || port > 65000 {
		return errors.New("invalid port")
	}
	bind := os.Getenv("RELAY_IP")
	if net.ParseIP(bind) == nil {
		return errors.New("invalid VPN interface IP")
	}
	m := mapper{gateway: "10.2.0.1:5351"}
	udp, err := net.ListenPacket("udp4", fmt.Sprintf("0.0.0.0:%d", port))
	if err != nil {
		return err
	}
	defer udp.Close()
	tcp, err := net.Listen("tcp4", fmt.Sprintf("0.0.0.0:%d", port))
	if err != nil {
		return err
	}
	defer tcp.Close()
	public, err := m.request(1, port, 0, 60)
	if err != nil {
		return err
	}
	defer m.request(1, port, public, 0)
	if _, err = m.request(2, port, public, 60); err != nil {
		return err
	}
	defer m.request(2, port, public, 0)
	gen := &generator{m: m, ip: ip, bind: bind, first: port + 1, last: port + 32}
	server, err := turn.NewServer(turn.ServerConfig{
		Realm: "fjorddrop",
		AuthHandler: func(username, realm string, src net.Addr) ([]byte, bool) {
			expires, e := strconv.ParseInt(strings.SplitN(username, ":", 2)[0], 10, 64)
			if e != nil || expires < time.Now().Unix() || expires > time.Now().Unix()+21660 {
				return nil, false
			}
			mac := hmac.New(sha1.New, secret)
			mac.Write([]byte(username))
			password := base64.StdEncoding.EncodeToString(mac.Sum(nil))
			return turn.GenerateAuthKey(username, realm, password), true
		},
		PacketConnConfigs: []turn.PacketConnConfig{{PacketConn: udp, RelayAddressGenerator: gen, PermissionHandler: allowedPeer}},
		ListenerConfigs:   []turn.ListenerConfig{{Listener: tcp, RelayAddressGenerator: gen, PermissionHandler: allowedPeer}},
	})
	if err != nil {
		return err
	}
	defer server.Close()
	writeStatus := func() error {
		b, _ := json.Marshal(map[string]any{"host": ip.String(), "port": public, "local_port": port, "checked_at": time.Now().Unix()})
		if e := os.WriteFile("/state/status.new", b, 0600); e != nil {
			return e
		}
		return os.Rename("/state/status.new", "/state/status.json")
	}
	defer os.Remove("/state/status.json")
	if err = writeStatus(); err != nil {
		return err
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-signals:
			return nil
		case <-ticker.C:
			if _, err = m.request(1, port, public, 60); err != nil {
				return err
			}
			if _, err = m.request(2, port, public, 60); err != nil {
				return err
			}
			if err = writeStatus(); err != nil {
				return err
			}
		}
	}
}
