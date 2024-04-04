#!/usr/bin/env python3

import re
import sys
import subprocess
import os
import getopt
import vcf
import Bio
import urllib.parse
from os import path
from Bio import SeqIO
from Bio import AlignIO
from io import StringIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
import subprocess


def main():
    params = parseArgs()

    # check if regions file
    regions = dict()
    if params.regfile:
        i = 1
        for r in read_regions(params.regfile):
            regions["locus_" + str(i)] = r
            i += 1
    elif params.region:
        if params.regname:
            regions[params.regname] = ChromRegion(params.region)
        else:
            params.regname = "locus_1"
            regions["locus_1"] = ChromRegion(params.region)

    # Grab reference sequence first
    reference = dict()
    print("Reading reference sequence from", params.ref)
    for contig in read_fasta(params.ref):
        name = contig[0].split()[0]
        fout = "contig_" + str(name) + ".fasta"
        if params.region:   # YL
            fout = "contig_" + str(regions[params.regname].chr) + "_" + str(
                regions[params.regname].start) + "-" + str(regions[params.regname].end) + ".fasta"
        # print("Checking if",fout,"exists")
        if path.exists(fout) and params.force == False:
            print("Output file for contig already exists, skipping it:", fout)
        else:
            # don't forget: 0-based index here, 1-based in VCF
            reference[name] = contig[1]

    # read in samples
    vfh = vcf.Reader(filename=params.vcf)
    samples = list()
    sampleMask = dict()  # dict of dict of sets
    for samp in vfh.samples:
        s = samp.split(".")[0]
        if s not in samples:
            samples.append(s)
        # if s not in sampleMask:
        # 	sampleMask[s] = dict()

    print("Found samples:", samples)

    # Get mask sites for each sample
    if len(reference) < 1:
        print("No contigs found.")
        sys.exit(0)

    if params.pileupMask:
        for maskFile in params.mpileup:
            # print(maskFile)
            base = os.path.basename(maskFile)
            samp = base.split(".")[0]
            if samp not in sampleMask:
                sampleMask[samp] = dict()

            # print(samp)
            with open(maskFile, 'r') as PILEUP:
                try:
                    for l in PILEUP:
                        l = l.strip()
                        if not l:
                            continue
                        line = l.split()
                        if len(line) < 4:
                            print("Warning in file",
                                  maskFile, " (bad line):", l)
                        else:
                            chrom = line[0]
                            pos = int(line[1]) - 1
                            depth = int(line[3])
                            # skip if this isn't the targeted chromosome
                            if params.region:
                                if regions[params.regname].chr != chrom:
                                    continue
                                if not (regions[params.regname].start <= pos + 1 <= regions[params.regname].end):
                                    continue
                            if depth < params.cov:
                                # Add to sampleMask
                                if chrom not in sampleMask[samp]:
                                    sampleMask[samp][chrom] = set()
                                sampleMask[samp][chrom].add(pos)

                except IOError as e:
                    print("Could not read file %s: %s" % (maskFile, e))
                    sys.exit(1)
                except Exception as e:
                    print("Unexpected error reading file %s: %s" %
                          (maskFile, e))
                    sys.exit(1)
                finally:
                    PILEUP.close()

        print("Found mask files:", sampleMask.keys())

    for contig, sequence in reference.items():
        # outFas = "contig_" + str(contig) + ".fasta"
        # if params.region:
        #     outFas = "contig_" + str(regions[params.regname].chr) + "_" + str(
        #         regions[params.regname].start) + "-" + str(regions[params.regname].end) + ".fasta"
        # with open(outFas, 'w') as fh:
        #     fh.close()  # clear exist file
        # loop through each region - YL
        for locus, locus_region in regions.items():
            #print(locus_region.gene)
            if contig != regions[locus].chr:
                continue
            
            outFas = f"{locus_region.gene}.fasta"
            
            outputs = dict()
            for samp in samples:
                outputs[samp] = str()
            spos = 0
            epos = len(sequence)
            spos = regions[locus].start - 1
            epos = regions[locus].end - 1
            if epos > len(sequence) or spos > (len(sequence)):
                spos = 0
                epos = len(sequence)
                print(
                    "WARNING: Specified region outside of bounds: Using full contig", contig)
            # print(spos, " ", epos)

            # loop through each nuc position in contig
            for nuc in range(spos, epos):
                this_pos = dict()
                for samp in samples:
                    this_pos[samp] = str()
                ref = None
                # For each sequence:
                # 1. check if any samples have VCF data (write VARIANT)
                for rec in vfh.fetch(contig, nuc, nuc + 1):
                    # print(rec.samples)
                    if int(rec.POS) != int(nuc + 1):
                        continue
                        # sys.exit()
                    if not ref:
                        ref = rec.REF
                    elif ref != rec.REF:
                        print("Warning! Reference alleles don't match at position %s (contig %s): %s vs. %s" % (
                            rec.POS, contig, ref, rec.REF))
                    for ind in rec.samples:
                        name = ind.sample.split(".")[0]
                        if ind.gt_type:
                            if not this_pos[name]:
                                alleles = ind.gt_bases.replace("|", "/").split("/")
                                this_pos[name] = genotype_resolve(alleles, params.indel)

                            else:
                                alleles = ind.gt_bases.replace("|", "/").split("/")
                                this_pos[name] = genotype_resolve(alleles, params.indel, this_pos[name])

                            # gt = "".join(sort(ind.gt_bases.split("/")))
                            # print(ind.sample, " : ", ind.gt_bases)

                # 3 insert Ns for masked samples at this position
                if params.pileupMask:
                    for samp in samples:
                        if samp in sampleMask:
                            if contig in sampleMask[samp] and nuc in sampleMask[samp][contig]:
                                this_pos[samp] = "N"

                # 4 if no allele chosen, write REF allele
                # use REF from VCF if possible, else pull from sequence
                for samp in samples:
                    if not this_pos[samp] or this_pos[samp] == "":
                        if not ref:
                            this_pos[samp] = sequence[nuc]
                        else:
                            this_pos[samp] = ref

                # 5. if there are insertions,  perform alignment to insert gaps
                l = None
                align = False
                for key in this_pos:
                    if not l:
                        l = len(this_pos[key])
                    else:
                        if l != len(this_pos[key]):
                            # otherwise, set for alignment
                            align = True

                # print(this_pos)
                if align == True:
                    old = this_pos
                    try:
                        # print("aligning")
                        # print(this_pos)
                        this_pos = clustalo_align(this_pos)
                        # print(this_pos)
                    except ValueError as e:
                        print("Somethign went wrong with MUSCLE call:", e)
                        this_pos = old

                # 6 replace indels with Ns if they were masked, or pad them if removed by MUSCLE
                maxlen = 1
                for key in this_pos:
                    if len(this_pos[key]) > maxlen:
                        maxlen = len(this_pos[key])

                for samp in samples:
                    if samp in this_pos:
                        if maxlen > 1:
                            if samp in sampleMask:
                                if contig in sampleMask[samp] and nuc in sampleMask[samp][contig]:
                                    new = repeat_to_length("N", maxlen)
                                    this_pos[samp] = new
                                    # print("Set:",this_pos)

                    else:
                        if samp in sampleMask and contig in sampleMask[samp] and nuc in sampleMask[samp][contig]:
                            new = repeat_to_length("N", maxlen)
                            this_pos[samp] = new
                            # print("Set:",this_pos)
                        else:
                            # sample was a multiple-nucleotide deletion
                            new = repeat_to_length("-", maxlen)
                            this_pos[samp] = new
                            # print("Set:",this_pos)

                # 7 Make sure nothing wonky happened
                p = False
                pis = False
                l = None
                a = None
                for samp in samples:
                    if samp in this_pos:
                        if not l:
                            l = len(this_pos[samp])
                        else:
                            if l != len(this_pos[samp]):
                                p = True
                    else:
                        p = True
                    # if not a:
                    # 	if this_pos[key] != "N":
                    # 		a = this_pos[key]
                    # else:
                    # 	if this_pos[key] != "N" and this_pos[key] != a:
                    # 		pis = True
                if p == True:
                    print("Warning: Something went wrong!")
                    print("Position:", nuc)
                    print(this_pos)
                # if pis==True:
                # 	print(this_pos)

                # 8 add new bases to output string for contig/region
                for samp in samples:
                    outputs[samp] += this_pos[samp]

            with open(outFas, 'a') as fh:
                try:
                    for sample in outputs:
                        # spicific region name - YL
                        to_write = ">" + str(contig) + ":" + str(spos + 1) + "-" + str(epos + 1) + "_" + str(sample) + "\n" + \
                            outputs[sample] + "\n"
                        fh.write(to_write)
                except IOError as e:
                    print("Could not read file:", e)
                    sys.exit(1)
                except Exception as e:
                    print("Unexpected error:", e)
                    sys.exit(1)
                finally:
                    fh.close()

# Function to split GFF attributes


def splitAttributes(a):
    ret = {}
    for thing in a.split(";"):
        stuff = thing.split("=")
        if len(stuff) != 2:
            continue  # Eats error silently, YOLO
        key = stuff[0].lower()
        value = stuff[1].lower()
        ret[key] = value
    return ret

# Class for holding GFF Record data, no __slots__


class GFFRecord():
    def __init__(self, things):
        self.seqid = "NULL" if things[0] == "." else urllib.parse.unquote(
            things[0])
        self.source = "NULL" if things[1] == "." else urllib.parse.unquote(
            things[1])
        self.type = "NULL" if things[2] == "." else urllib.parse.unquote(
            things[2])
        self.start = "NULL" if things[3] == "." else int(things[3])
        self.end = "NULL" if things[4] == "." else int(things[4])
        self.score = "NULL" if things[5] == "." else float(things[5])
        self.strand = "NULL" if things[6] == "." else urllib.parse.unquote(
            things[6])
        self.phase = "NULL" if things[7] == "." else urllib.parse.unquote(
            things[7])
        self.attributes = {}
        if things[8] != "." and things[8] != "":
            self.attributes = splitAttributes(urllib.parse.unquote(things[8]))

    def getAlias(self):
        """Returns value of alias if exists, and False if it doesn't exist"""
        if 'alias' in self.attributes:
            return self.attributes['alias']
        else:
            return False

# file format:
# 1 per line:
# chr1:1-1000
# ...


def read_regions(r):
    # with open(r, 'w') as fh:
    with open(r, 'r') as fh:    # read file not write file - YL
        try:
            for line in fh:
                line = line.strip()  # strip leading/trailing whitespace
                if not line:  # skip empty lines
                    continue
                yield(ChromRegion(line))
        finally:
            fh.close()


# function to read a GFF file
# Generator function, yields individual elements
def read_gff(g):
    bad = 0  # tracker for if we have bad lines
    gf = open(g)
    try:
        with gf as file_object:
            for line in file_object:
                if line.startswith("#"):
                    continue
                line = line.strip()  # strip leading/trailing whitespace
                if not line:  # skip empty lines
                    continue
                things = line.split("\t")  # split lines
                if len(things) != 9:
                    if bad == 0:
                        print(
                            "Warning: GFF file does not appear to be standard-compatible. See https://github.com/The-Sequence-Ontology/Specifications/blob/master/gff3.md")
                        bad = 1
                        continue
                    elif bad == 1:
                        sys.exit(
                            "Fatal error: GFF file does not appear to be standard-compatible. See https://github.com/The-Sequence-Ontology/Specifications/blob/master/gff3.md")
                # line = utils.removeURL(line) #Sanitize any URLs out
                rec = GFFRecord(things)

                yield(rec)
    finally:
        gf.close()


def repeat_to_length(string_to_expand, length):
    return (string_to_expand * (int(length / len(string_to_expand)) + 1))[:length]

# return dict alignment from dict of sequences, run via MUSCLE


import subprocess

def clustalo_align(aln):
    max_len = 0
    for key, seq in aln.items():
        max_len = max(max_len, len(seq))
    records = Bio.Align.MultipleSeqAlignment([])
    for key, seq in aln.items():
        seq = seq.ljust(max_len, '-')
        records.append(SeqRecord(Seq(seq), id=key, description=''))  # empty description

    # write FASTA as a string in memory
    handle = StringIO()
    SeqIO.write(records, handle, "fasta")
    data = handle.getvalue()

    # invoke clustalo with Popen
    process = subprocess.Popen(['clustalo', '-i', '-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate(input=data.encode())

    # Check if there was an error running clustalo
    if process.returncode != 0:
        print("Error running Clustal Omega:")
        print(stderr.decode())
    else:
        # clustalo was successful, so try to read the output as an alignment
        try:
            data = StringIO(stdout.decode())
            alignment = AlignIO.read(data, "fasta")
        except Exception as e:
            print("Error reading Clustal Omega output as an alignment:")
            print(e)
    new = dict()
    for record in alignment:
        new[record.id] = str(record.seq)
    return(new)


# internal function to resolve genotypes from VCF file
def genotype_resolve(l, indelPriority, e=None):
    if e:
        l.append(e)
    var = set()
    indel = set()
    if len(l) == 1:
        return(l[0])
    else:
        for gt in l:
            if len(gt) > 1 or gt == "*":
                if gt == "*":
                    indel.add("-")
                else:
                    indel.add(gt)
            else:
                var.add(gt)

        if indelPriority:
            if len(indel) == 1:
                return(next(iter(indel)))
            if len(indel) > 1:
                minlen = int()
                for i in indel:
                    if not minlen:
                        minlen = len(i)
                    else:
                        if minlen > len(i):
                            minlen = len(i)
                for i in indel:
                    if minlen == len(i):
                        return(i)
        if len(var) == 1:
            return(next(iter(var)))
        elif len(var) > 1:
            gl = list(var)
            gl.sort()
            gt = "".join(gl)
            return(reverse_iupac_case(gt))
        elif len(var) < 1:
            if len(indel) == 1:
                return(next(iter(indel)))
            elif len(indel) > 1:
                minlen = int()
                for i in indel:
                    if not minlen:
                        minlen = len(i)
                    else:
                        if minlen > len(i):
                            minlen = len(i)
                for i in indel:
                    if minlen == len(i):
                        return(i)


# Function to translate a string of bases to an iupac ambiguity code, retains case
def reverse_iupac_case(char):
    iupac = {
        'A': 'A',
        'N': 'N',
        '-': '-',
        'C': 'C',
        'G': 'G',
        'T': 'T',
        'AG': 'R',
        'CT': 'Y',
        'AC': 'M',
        'GT': 'K',
        'AT': 'W',
        'CG': 'S',
        'CGT': 'B',
        'AGT': 'D',
        'ACT': 'H',
        'ACG': 'V',
        'ACGT': 'N',
        'a': 'a',
        'n': 'n',
        'c': 'c',
        'g': 'g',
        't': 't',
        'ag': 'r',
        'ct': 'y',
        'ac': 'm',
        'gt': 'k',
        'at': 'w',
        'cg': 's',
        'cgt': 'b',
        'agt': 'd',
        'act': 'h',
        'acg': 'v',
        'acgt': 'n'
    }
    return iupac[char]


# Read genome as FASTA. FASTA header will be used
# This is a generator function
# Doesn't matter if sequences are interleaved or not.
def read_fasta(fas):
    fh = open(fas)
    try:
        with fh as file_object:
            contig = ""
            seq = ""
            for line in file_object:
                line = line.strip()
                if not line:
                    continue
                # print(line)
                if line[0] == ">":  # Found a header line
                    # If we already loaded a contig, yield that contig and
                    # start loading a new one
                    if contig:
                        yield([contig, seq])  # yield
                        contig = ""  # reset contig and seq
                        seq = ""
                    contig = (line.replace(">", ""))
                else:
                    seq += line
        # Iyield last sequence, if it has both a header and sequence
        if contig and seq:
            yield([contig, seq])
    finally:
        fh.close()


class ChromRegion():
    def __init__(self, s):
        stuff = re.split(':|-', s)
        self.gene = str(stuff[0])
        self.chr = str(stuff[1])
        self.start = int(stuff[2])
        self.end = int(stuff[3])


# Object to parse command-line arguments
class parseArgs():
    def __init__(self):
        # Define options
        try:
            options, remainder = getopt.getopt(sys.argv[1:], 'f:v:m:c:R:hs:f:g:dF:r:',
                                               ["vcf=", "help", "ref=", "fasta=", "mpileup=", "cov=", "reg=", "indel",
                                                "regfile=", "gff=", "dp", "force", "flank=", "id_field=",
                                                "regname="])
        except getopt.GetoptError as err:
            print(err)
            self.display_help(
                "\nExiting because getopt returned non-zero exit status.")
        # Default values for params
        # Input params
        self.vcf = None
        self.ref = None
        self.pileupMask = False
        self.mpileup = list()
        self.dp = False
        self.cov = 1
        self.flank = 0
        self.region = None
        self.regname = None
        self.regfile = None
        self.indel = False
        self.force = False

        # First pass to see if help menu was called
        for o, a in options:
            if o in ("-h", "-help", "--help"):
                self.display_help("Exiting because help menu was called.")

        # Second pass to set all args.
        for opt, arg_raw in options:
            arg = arg_raw.replace(" ", "")
            arg = arg.strip()
            opt = opt.replace("-", "")
            # print(opt,arg)
            if opt == "v" or opt == "vcf":
                self.vcf = arg
            elif opt == "c" or opt == "cov":
                self.cov = int(arg)
            elif opt == "m" or opt == "mpileup":
                self.pileupMask = True
                self.mpileup.append(arg)
            elif opt == "d" or opt == "dp":
                self.dp = True
            elif opt == "f" or opt == "fasta":
                self.ref = arg
            elif opt == "r" or opt == "reg":
                self.region = arg
            elif opt == "R" or opt == "regfile":
                self.regfile = arg
            elif opt == "F" or opt == "flank":
                self.flank = int(arg)
            elif opt == "h" or opt == "help":
                pass
            elif opt == "regname":
                self.regname = arg
            elif opt == 'indel':
                self.indel = True
            elif opt == "force":
                self.force = True
            else:
                assert False, "Unhandled option %r" % opt

        # Check manditory options are set
        if not self.vcf:
            self.display_help("Must provide VCF file <-v,--vcf>")
        if not self.ref:
            #self.display_help("Must provide reference FASTA file <-r,--ref")
            # fix manual - YL
            self.display_help("Must provide reference FASTA file <-f,--fasta")
        # if self.regfile and len(self.region) > 0:
        if self.regfile and self.region != None:    # fix bug - YL
            self.display_help("Cannot use both -R and -r")
        if not self.regfile and not self.region and not self.gff:
            self.display_help("No regions selected")

    def display_help(self, message=None):
        if message is not None:
            print()
            print(message)
        print("\nvcf2msa.py\n")
        print("Contact:Tyler K. Chafin, tyler.chafin@colorado.edu")
        print("Description: Builds multiple sequence alignments from multi-sample VCF")

        print("""
	Mandatory arguments:
		-f,--fasta	: Reference genome file (FASTA)
		-v,--vcf	: VCF file containing genotypes

	Masking arguments:
		-c,--cov	: Minimum coverage to call a base as REF [default=1]
		-m,--mpileup: Per-sample mpileup file (one for each sample) for ALL sites (-aa in samtools)
		-d,--dp		: Toggle on to get depth from per-sample DP scores in VCF file

	Region selection arguments:
		Must use one of:
		-r,--reg	: Region to sample (e.g. chr1:1-1000)
		-R,--regfile: Text file containing regions to sample (1 per line, e.g. chr1:1-1000)

	Other arguments:
		-F,--flank	: Integer representing number of bases to add on either sides of selected regions [default=0]
		--indel		: In cases where indel conflicts with SNP call, give precedence to indel
		--force		: Overwrite existing alignment files
		-h,--help	: Displays help menu
""")
        print()
        sys.exit()


# Call main function
if __name__ == '__main__':
    main()
