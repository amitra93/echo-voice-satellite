//go:build server

// afe_helper is a standalone diagnostic wrapper around the production helper
// protocol. Production uses the firmware binary's --afe-helper mode instead.
package main

import (
	"flag"
	"github.com/wilbowes/EchoMuse/internal/afeipc"
	"log"
)

func main() {
	lib := flag.String("lib", "libOpenSLES.so", "OpenSL ES library path or soname")
	flag.Parse()
	if err := afeipc.RunHelper(*lib); err != nil {
		log.Print(err)
	}
}
