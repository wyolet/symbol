// Package user is a fixture for end-to-end Go indexing tests.
// Keep this file stable — the test asserts exact symbol kinds, ranges,
// and ref shapes against it.
package user

import (
	"fmt"
	"strings"
)

const MaxRetries = 3

var DefaultName = "anon"

type User struct {
	Name string
}

func (u *User) Greet() string {
	greeting := strings.ToUpper("hello, " + u.Name)
	fmt.Println(greeting)
	return greeting
}

func New(name string) *User {
	return &User{Name: name}
}
