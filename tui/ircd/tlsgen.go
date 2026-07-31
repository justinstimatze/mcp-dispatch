// tlsgen.go — `--init-tls`: a certificate, in one command.
//
// Requiring TLS on every TCP listener is only reasonable if getting one is
// trivial, so this generates a self-signed cert for the hosts you'd actually
// bind. Self-signed is the right shape here: there is no CA that can vouch for
// "the IRC gateway on my laptop", and the client is you. What replaces the CA
// is the fingerprint — printed on generation and on every start, so a client
// can pin it and notice if it ever changes.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// certValidity is deliberately short of a decade. A gateway certificate that
// outlives the machine it was made for is a certificate nobody remembers
// generating.
const certValidity = 2 * 365 * 24 * time.Hour

// fingerprint is the SHA-256 of the DER certificate, formatted the way IRC
// clients show it — this is what a user pins instead of trusting a CA.
func fingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	hexs := hex.EncodeToString(sum[:])
	var b strings.Builder
	for i := 0; i < len(hexs); i += 2 {
		if i > 0 {
			b.WriteByte(':')
		}
		b.WriteString(strings.ToUpper(hexs[i : i+2]))
	}
	return b.String()
}

// fingerprintFile reads a PEM certificate and returns its fingerprint.
func fingerprintFile(path string) (string, error) {
	raw, err := os.ReadFile(path) //nolint:gosec // operator-configured path
	if err != nil {
		return "", err
	}
	block, _ := pem.Decode(raw)
	if block == nil || block.Type != "CERTIFICATE" {
		return "", fmt.Errorf("%s is not a PEM certificate", path)
	}
	return fingerprint(block.Bytes), nil
}

// WriteSelfSignedCert generates an ECDSA P-256 key and a self-signed
// certificate covering localhost, the loopback addresses and this host's name.
// P-256 rather than Ed25519: every IRC client that speaks TLS handles it, which
// is the whole point of choosing a curve for a compatibility surface like this.
func WriteSelfSignedCert(certPath, keyPath string, extraHosts []string, force bool) (string, error) {
	for _, p := range []string{certPath, keyPath} {
		if _, err := os.Stat(p); err == nil && !force {
			return "", fmt.Errorf("%s already exists (use --force to replace it)", p)
		}
	}
	if err := os.MkdirAll(filepath.Dir(certPath), 0o700); err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(keyPath), 0o700); err != nil {
		return "", err
	}

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return "", err
	}
	serialMax := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, serialMax)
	if err != nil {
		return "", err
	}

	dns := []string{"localhost"}
	ips := []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")}
	if host, err := os.Hostname(); err == nil && host != "" && host != "localhost" {
		dns = append(dns, host)
	}
	for _, h := range extraHosts {
		h = strings.TrimSpace(h)
		if h == "" {
			continue
		}
		if ip := net.ParseIP(h); ip != nil {
			ips = append(ips, ip)
		} else {
			dns = append(dns, h)
		}
	}

	tmpl := x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "mcp-dispatch IRC gateway"},
		NotBefore:             time.Now().Add(-time.Hour), // tolerate mild clock skew
		NotAfter:              time.Now().Add(certValidity),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true, // self-signed: it is its own issuer
		DNSNames:              dns,
		IPAddresses:           ips,
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &key.PublicKey, key)
	if err != nil {
		return "", err
	}

	// The certificate is public; the key is not. Write the key through O_EXCL
	// into a fresh inode so we never write a secret through a symlink someone
	// left in place.
	if err := os.WriteFile(certPath, pem.EncodeToMemory(
		&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644); err != nil { //nolint:gosec // a certificate is public
		return "", err
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return "", err
	}
	_ = os.Remove(keyPath)
	kf, err := os.OpenFile(keyPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return "", err
	}
	defer kf.Close()
	if err := pem.Encode(kf, &pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}); err != nil {
		return "", err
	}
	return fingerprint(der), nil
}
