CXX ?= c++
PKG_CONFIG ?= pkg-config
CXXFLAGS ?= -O3 -std=c++17 -Wall -Wextra -Wpedantic
CPPFLAGS ?=
LDFLAGS ?=
TARGET ?= exportLeoRouteFrames

CODEC_DIR := ../../leosatellites/src/networklayer/configurator/ipv4
SOURCES := exportLeoRouteFrames.cc $(CODEC_DIR)/LeoKPathSnapshot.cc \
           $(CODEC_DIR)/LeoRouteSnapshot.cc $(CODEC_DIR)/LeoKShortestPaths.cc
HEADERS := $(CODEC_DIR)/LeoKPathSnapshot.h $(CODEC_DIR)/LeoRouteSnapshot.h \
           $(CODEC_DIR)/LeoKShortestPaths.h
IGRAPH_CFLAGS := $(shell $(PKG_CONFIG) --cflags igraph)
IGRAPH_LIBS := $(shell $(PKG_CONFIG) --libs igraph)

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SOURCES) $(HEADERS) Makefile
	$(PKG_CONFIG) --exists igraph
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(IGRAPH_CFLAGS) -I$(CODEC_DIR) $(SOURCES) $(LDFLAGS) $(IGRAPH_LIBS) -o $@

clean:
	rm -f $(TARGET)
